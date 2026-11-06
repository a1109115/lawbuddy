# -*- coding: utf-8 -*-
"""M3 评测器：实质法律质量金标集（05-阅卷Agent升级方案 §11.3）。

用法（services/core 目录下）：
    python -m evals.m3_gold_standard            # 确定性自检（金标集与机制层）
    python -m evals.m3_gold_standard --live     # 附加真实模型跑分（需已配置模型密钥）

定位（§11.3 明确约束）：不以 mock 最终回答代替真实质量评测。本评测做两件事：

1. 确定性自检：8 个金标场景在数据与机制层面成立——
   - 每条金标命题的 quote 均可经 citations.verify_quote 逆校验到夹具原文页；
   - 冲突场景双方来源均可定位且能进 Claim 冲突关系；
   - 同源重复场景两份材料正文逐字一致（可被识别为同一来源）；
   - 低 OCR、缺页、版本替换分别触发 LOW_OCR_CONFIDENCE / QUOTE_MISMATCH，
     旧版本 quote 对新版本页失效；
   - 信息不足场景缺失材料在检索层确实不存在；
   - direct / focused / full_review 深度判定与用户意图一致（含显式选材与
     合同审查模式固定 focused 的优先级）。
2. 真实跑分脚手架（--live）：用真实网关逐场景跑任务，机械统计金标召回
   （supported Claim 命中金标 doc:page）、冲突双方来源呈现与任务深度；
   未配置密钥时显式 SKIP，不伪造结果。语义质量的六维人工评分仍需人工
   复核（§11.3 评分维度），不由本评测替代，也不由模型辅助评分替代。

报告：控制台摘要 + evals/reports/m3_gold_standard_<ts>.json。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 金标场景夹具（§11.3 八类覆盖）
#gold 文本刻意写成自然卷宗语句；金标 quote 一律取页内不含首尾标点的子串，
# 保证 clean_snippet/normalize_text 不会改变其子串性质。
# ---------------------------------------------------------------------------

SCENARIOS: list[dict[str, Any]] = [
    {
        "key": "criminal",
        "title": "金标-刑事诈骗指控",
        "prompt": "全卷阅卷：系统梳理本案全部证据材料，评价指控证据体系与辩方线索",
        "expectDepth": "full_review",
        "docs": {
            "indictment": ("起诉书", {1: "被告人张某虚构工程分包事实，与王某签订虚假合同，骗取工程款共计人民币二百万元。"}),
            "interrogation": ("讯问笔录", {1: "问：这份工程合同是谁提出签的？答：是王某先找我，说用假合同从公司套钱出来。"}),
            "witness_li": ("证人证言（李某）", {1: "证言内容：我听王某说他和张某签了假合同套钱。"}),
            "witness_zhou": ("证人证言（周某）", {1: "证言内容：我听王某说他和张某签了假合同套钱。"}),
            "repair_log": ("设备维修记录", {1: "2024年4月15日 14:00-18:00 张某在城东维修点更换设备主板并签字确认。"}),
        },
        "golds": [
            {"doc": "indictment", "page": 1, "quote": "虚构工程分包事实，与王某签订虚假合同，骗取工程款共计人民币二百万元", "note": "指控要素"},
            {"doc": "interrogation", "page": 1, "quote": "是王某先找我，说用假合同从公司套钱出来", "note": "被告人供述"},
            {"doc": "witness_li", "page": 1, "quote": "我听王某说他和张某签了假合同套钱", "note": "同源证言A（转述王某）"},
            {"doc": "witness_zhou", "page": 1, "quote": "我听王某说他和张某签了假合同套钱", "note": "同源证言B（与A逐字一致）"},
            {"doc": "repair_log", "page": 1, "quote": "2024年4月15日 14:00-18:00 张某在城东维修点更换设备主板并签字确认", "note": "反向证据（与指控时间冲突）"},
        ],
    },
    {
        "key": "civil",
        "title": "金标-民商事价款请求权",
        "prompt": "分析本案价款请求权的构成要件、证明责任与时效争点",
        "expectDepth": "focused",
        "docs": {
            "contract": ("买卖合同", {1: "第三条 买受人应于收到全部设备并验收合格后三十日内支付价款人民币五十万元。"}),
            "acceptance": ("送货验收单", {1: "兹确认全部设备已于二〇二五年五月二十日交付并验收合格。"}),
            "demand": ("催款函", {1: "兹催告贵司于收函后十日内支付剩余货款人民币五十万元及逾期利息。"}),
        },
        "golds": [
            {"doc": "contract", "page": 1, "quote": "买受人应于收到全部设备并验收合格后三十日内支付价款人民币五十万元", "note": "请求权基础"},
            {"doc": "acceptance", "page": 1, "quote": "全部设备已于二〇二五年五月二十日交付并验收合格", "note": "交付要件事实"},
            {"doc": "demand", "page": 1, "quote": "兹催告贵司于收函后十日内支付剩余货款人民币五十万元及逾期利息", "note": "时效中断事实"},
        ],
    },
    {
        "key": "conflict",
        "title": "金标-陈述与客观记载冲突",
        "prompt": "分析双方关于设备是否安装调试完成的陈述冲突，并列出双方来源原文",
        "expectDepth": "focused",
        "docs": {
            "chat": ("微信聊天记录", {1: "原告代理人：设备已于六月一日全部安装调试完成，贵司现场确认无误。"}),
            "acceptance_form": ("验收单", {1: "验收结论：两台设备未通过测试，本次验收未通过，不予签收。"}),
        },
        "golds": [
            {"doc": "chat", "page": 1, "quote": "设备已于六月一日全部安装调试完成，贵司现场确认无误", "note": "当事人陈述一方"},
            {"doc": "acceptance_form", "page": 1, "quote": "两台设备未通过测试，本次验收未通过，不予签收", "note": "客观记载一方"},
        ],
    },
    {
        "key": "same_origin",
        "title": "金标-同源重复材料",
        "prompt": "分析两份情况说明能否相互印证工程量结算事实",
        "expectDepth": "focused",
        "docs": {
            "copy_a": ("情况说明（复印件）", {1: "兹说明二〇二四年五月双方已完成全部工程量结算，结算金额为人民币三百八十万元。"}),
            "copy_b": ("情况说明（扫描件）", {1: "兹说明二〇二四年五月双方已完成全部工程量结算，结算金额为人民币三百八十万元。"}),
        },
        "golds": [
            {"doc": "copy_a", "page": 1, "quote": "双方已完成全部工程量结算，结算金额为人民币三百八十万元", "note": "同源文本（复印件）"},
            {"doc": "copy_b", "page": 1, "quote": "双方已完成全部工程量结算，结算金额为人民币三百八十万元", "note": "同源文本（扫描件，与复印件逐字一致）"},
        ],
    },
    {
        "key": "evidence_quality",
        "title": "金标-低OCR缺页与版本替换",
        "prompt": "分析本案关键书证的证明力与瑕疵",
        "expectDepth": "focused",
        "docs": {
            "ocr_scan": ("手写送货单（扫描件）", {1: "今收到设备配件一批共计人民币十二万元整"}),
            "torn": ("讯问笔录（缺页）", {1: "第一次讯问：张某对签订合同经过作了供述。", 2: "", 3: "第三次讯问：张某对款项去向作了供述。"}),
            "versioned": ("设备采购合同", {1: "第一条 合同总价为人民币三十万元整（小写：¥300,000）。"}),
        },
        "golds": [],
    },
    {
        "key": "insufficient",
        "title": "金标-信息不足",
        "prompt": "分析本案已交付货物的证据是否充分",
        "expectDepth": "focused",
        "docs": {
            "contract_only": ("买卖合同", {1: "第一条 甲方向乙方出售数控机床一台，价款人民币八十万元，交付方式另行约定。"}),
        },
        "golds": [],
    },
    {
        "key": "competing",
        "title": "金标-借贷与货款竞争解释",
        "prompt": "分析该笔二十万元转账属于借款还是货款，并列出支持与反对两方面的原文依据",
        "expectDepth": "focused",
        "docs": {
            "iou": ("借条", {1: "今借到赵某人民币二十万元整，借期六个月，到期一次性归还。借款人：钱某，二〇二五年一月五日。"}),
            "transfer": ("银行转账回单", {1: "二〇二五年一月五日 赵某向钱某转账人民币二十万元整，附言：设备货款。"}),
        },
        "golds": [
            {"doc": "iou", "page": 1, "quote": "今借到赵某人民币二十万元整，借期六个月，到期一次性归还", "note": "借贷合意（支持解释）"},
            {"doc": "transfer", "page": 1, "quote": "赵某向钱某转账人民币二十万元整，附言：设备货款", "note": "货款附言（最强替代解释）"},
        ],
    },
]

# S8 深度选择：_derive_task_contract 为纯函数，直接断言意图映射
DEPTH_CASES: list[tuple[dict[str, Any], str]] = [
    ({"mode": "legal_review", "request": "送货单第1页的签收人是谁？"}, "direct"),
    ({"mode": "legal_review", "request": "分析本案价款是否已支付的争点"}, "focused"),
    ({"mode": "legal_review", "request": "全卷阅卷：系统梳理全部证据材料与争议焦点"}, "full_review"),
    # 用户显式选定材料：即使请求文本像单点问题，也按 focused（不擅自扩大也不降级为无范围 direct）
    ({"mode": "legal_review", "request": "这份合同的违约责任条款写了什么", "selected_document_ids": ["doc-x"]}, "focused"),
    # 合同审查模式固定 focused（深度关键词不生效）
    ({"mode": "contract_review", "request": "全卷审查这份合同的付款条款"}, "focused"),
]


def _log(msg: str) -> None:
    print(msg, flush=True)


def _seed_all() -> dict[str, dict[str, Any]]:
    """逐场景建案并写入夹具，返回 {scenarioKey: {case, ids, task}}。"""
    from evals.m3_loop_baseline import _seed_docs
    from lawbuddy.agent.tasks import create_task
    from lawbuddy.storage import get_db

    seeded: dict[str, dict[str, Any]] = {}
    for sc in SCENARIOS:
        case, ids = _seed_docs(sc["title"], sc["docs"])
        task = create_task(case_id=case["id"], mode="legal_review", request_text=sc["prompt"])
        seeded[sc["key"]] = {"case": case, "ids": ids, "task": task}
    # 证据质量场景的三个机制夹具（OCR 置信度 / 版本替换）
    db = get_db()
    eq = seeded["evidence_quality"]
    with db.transaction() as conn:
        # 低 OCR 置信页：0.62 < LOW_OCR_CONFIDENCE(0.85)
        conn.execute(
            "UPDATE document_pages SET ocr_confidence = 0.62 WHERE version_id = 'ver-ocr_scan' AND page_no = 1"
        )
        # 版本替换：v2 总价五十万替换 v1 三十万，切换活动版本
        conn.execute(
            "INSERT OR REPLACE INTO document_versions(id, document_id, blob_uri, sha256, size_bytes, "
            "parse_status, page_count, created_at) VALUES (?, ?, ?, ?, 0, 'ready', 1, ?)",
            ("ver-versioned-v2", eq["ids"]["versioned"], "blob://ver-versioned-v2", "1" * 64,
             "2026-10-28T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO document_pages(id, version_id, page_no, text_content, "
            "text_quality, ocr_status, ocr_confidence) VALUES (?, ?, 1, ?, 'native', 'none', NULL)",
            ("ver-versioned-v2-p1", "ver-versioned-v2",
             "第一条 合同总价为人民币五十万元整（小写：¥500,000）。"),
        )
        conn.execute(
            "UPDATE documents SET active_version_id = 'ver-versioned-v2' WHERE id = ?",
            (eq["ids"]["versioned"],),
        )
        # FTS 同步到新版本文本（live 检索用；失败不阻断确定性断言）
        try:
            conn.execute(
                "DELETE FROM pages_fts WHERE document_id = ? AND page_no = 1",
                (eq["ids"]["versioned"],),
            )
            conn.execute(
                "INSERT INTO pages_fts(text_content, case_id, document_id, document_title, "
                "version_id, page_no, text_quality) VALUES (?, ?, ?, '设备采购合同', ?, 1, 'native')",
                ("第一条 合同总价为人民币五十万元整（小写：¥500,000）。",
                 eq["case"]["id"], eq["ids"]["versioned"], "ver-versioned-v2"),
            )
        except Exception:  # noqa: BLE001
            pass
    return seeded


# ---------------------------------------------------------------------------
# 确定性自检
# ---------------------------------------------------------------------------

def _deterministic_rows(seeded: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, bool]]:
    from lawbuddy.citations import judge_claim, mark_conflict, normalize_text, verify_quote
    from lawbuddy.agent.autonomous import _derive_task_contract
    from lawbuddy.search import search_pages

    rows: list[dict[str, Any]] = []
    gates: dict[str, bool] = {}

    # 1) 金标 quote 全部逆校验通过（S1/S2/S3/S4/S7）
    fails: list[str] = []
    total_golds = 0
    for sc in SCENARIOS:
        entry = seeded[sc["key"]]
        for g in sc["golds"]:
            total_golds += 1
            version_id = f"ver-{g['doc']}"
            if not verify_quote(normalize_text(g["quote"]), version_id, g["page"]):
                fails.append(f"{sc['key']}:{g['doc']}:{g['page']}")
    rows.append({"step": "goldQuotesVerify", "golds": total_golds, "fails": fails})
    gates["goldQuotesVerify"] = not fails and total_golds >= 14

    # 2) 冲突双方来源均可建 Claim 并进入冲突关系（S3）
    cf = seeded["conflict"]
    side_a = judge_claim(cf["task"]["id"], cf["case"]["id"], "原告主张设备已安装调试完成",
                         {"documentId": cf["ids"]["chat"], "versionId": "ver-chat", "pageNo": 1,
                          "snippet": "设备已于六月一日全部安装调试完成，贵司现场确认无误"})
    side_b = judge_claim(cf["task"]["id"], cf["case"]["id"], "验收单记载验收未通过",
                         {"documentId": cf["ids"]["acceptance_form"], "versionId": "ver-acceptance_form",
                          "pageNo": 1, "snippet": "两台设备未通过测试，本次验收未通过，不予签收"})
    linked = mark_conflict(side_a["claimId"], side_b["claimId"])
    ok_conflict = (side_a["claimStatus"] == "supported" and side_b["claimStatus"] == "supported"
                   and linked)
    rows.append({"step": "conflictBothSidesTracked",
                 "sideA": side_a["claimStatus"], "sideB": side_b["claimStatus"],
                 "conflictLinked": linked})
    gates["conflictBothSidesTracked"] = ok_conflict

    # 3) 同源重复可识别（S4：两页正文规范化后逐字一致，quote 在两页均可校验）
    same_quote = SCENARIOS[3]["golds"][0]["quote"]
    on_a = verify_quote(normalize_text(same_quote), "ver-copy_a", 1)
    on_b = verify_quote(normalize_text(same_quote), "ver-copy_b", 1)
    rows.append({"step": "sameOriginDetectable", "quoteOnCopyA": on_a, "quoteOnCopyB": on_b})
    gates["sameOriginDetectable"] = on_a and on_b

    # 4) 低 OCR → needs_human(LOW_OCR_CONFIDENCE)（S5）
    eq = seeded["evidence_quality"]
    low = judge_claim(eq["task"]["id"], eq["case"]["id"], "手写送货单载明收到配件一批十二万元",
                      {"documentId": eq["ids"]["ocr_scan"], "versionId": "ver-ocr_scan", "pageNo": 1,
                       "snippet": "今收到设备配件一批共计人民币十二万元整"})
    rows.append({"step": "lowOcrNeedsHuman", "status": low["claimStatus"], "reason": low.get("reasonCode")})
    gates["lowOcrNeedsHuman"] = low["claimStatus"] == "needs_human" and low.get("reasonCode") == "LOW_OCR_CONFIDENCE"

    # 5) 缺页不得被断言（S5：空页 verify 失败，伪造内容 → QUOTE_MISMATCH）
    torn_miss = verify_quote(normalize_text("此页记载了第二次讯问内容"), "ver-torn", 2)
    torn_claim = judge_claim(eq["task"]["id"], eq["case"]["id"], "第二次讯问候页内容",
                             {"documentId": eq["ids"]["torn"], "versionId": "ver-torn", "pageNo": 2,
                              "snippet": "此页记载了第二次讯问内容"})
    rows.append({"step": "missingPageUnassertable", "verifyOnEmptyPage": torn_miss,
                 "claimStatus": torn_claim["claimStatus"], "reason": torn_claim.get("reasonCode")})
    gates["missingPageUnassertable"] = (not torn_miss
                                        and torn_claim["claimStatus"] == "needs_human"
                                        and torn_claim.get("reasonCode") == "QUOTE_MISMATCH")

    # 6) 版本替换使旧 quote 失效（S5：旧价对新版本页 False，新价 True，旧引证落 QUOTE_MISMATCH）
    old_ok_v1 = verify_quote(normalize_text("合同总价为人民币三十万元整"), "ver-versioned", 1)
    old_ok_v2 = verify_quote(normalize_text("合同总价为人民币三十万元整"), "ver-versioned-v2", 1)
    new_ok_v2 = verify_quote(normalize_text("合同总价为人民币五十万元整"), "ver-versioned-v2", 1)
    stale_claim = judge_claim(eq["task"]["id"], eq["case"]["id"], "合同总价三十万元",
                              {"documentId": eq["ids"]["versioned"], "versionId": "ver-versioned-v2",
                               "pageNo": 1, "snippet": "合同总价为人民币三十万元整"})
    rows.append({"step": "versionSwapInvalidates", "oldQuoteOnV1": old_ok_v1, "oldQuoteOnV2": old_ok_v2,
                 "newQuoteOnV2": new_ok_v2, "staleClaimStatus": stale_claim["claimStatus"],
                 "reason": stale_claim.get("reasonCode")})
    gates["versionSwapInvalidates"] = (old_ok_v1 and not old_ok_v2 and new_ok_v2
                                       and stale_claim.get("reasonCode") == "QUOTE_MISMATCH")

    # 7) 信息不足场景夹具成立（S6：缺失材料在检索层确实不存在）
    ins = seeded["insufficient"]
    hits_pay = search_pages(ins["case"]["id"], "付款凭证", limit=5)
    hits_delivery = search_pages(ins["case"]["id"], "送货单", limit=5)
    rows.append({"step": "insufficientInfoWellFormed",
                 "paymentProofHits": len(hits_pay), "deliveryNoteHits": len(hits_delivery)})
    gates["insufficientInfoWellFormed"] = not hits_pay and not hits_delivery

    # 8) 深度选择与用户意图一致（S8，纯函数断言）
    depth_fails: list[str] = []
    for state, expected in DEPTH_CASES:
        got = _derive_task_contract(state)["taskDepth"]
        if got != expected:
            depth_fails.append(f"{state.get('request', '')[:16]}…→{got}（期望 {expected}）")
    rows.append({"step": "depthSelectionMatchesIntent", "cases": len(DEPTH_CASES), "fails": depth_fails})
    gates["depthSelectionMatchesIntent"] = not depth_fails

    return rows, gates


# ---------------------------------------------------------------------------
# 真实跑分脚手架（--live）
# ---------------------------------------------------------------------------

def _live_probe() -> tuple[bool, str]:
    """探测真实网关可用性。

    先查设置表确认密钥已配置（只判断密文行是否存在，不解密、不出网、不打印密钥）；
    未配置时直接 SKIP，避免在无密钥环境下发起必然失败的网络请求。已配置时用
    30 秒硬超时包住探测调用，异常只取类型与短消息。
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

    from lawbuddy.agent import model_gateway
    from lawbuddy.storage import get_profile_db

    try:
        # 模型配置（含 API Key）位于设备级 profile 库，不在工作空间库
        row = get_profile_db().query_one(
            "SELECT COUNT(*) AS n FROM settings WHERE key LIKE 'model.%ApiKey' "
            "AND value_json IS NOT NULL AND value_json != ''"
        )
        if not row or int(row["n"]) < 1:
            return False, "未配置模型 API Key（settings 无密钥行）：真实跑分待配置后执行"
    except Exception as exc:  # noqa: BLE001
        return False, f"读取密钥配置状态失败 {type(exc).__name__}: {str(exc)[:80]}"

    def _call() -> str:
        result = model_gateway.chat_with_tools(
            [{"role": "user", "content": "请只回复两个字：就绪"}], tools=None,
        )
        return str(result.get("content") or "").strip()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            content = pool.submit(_call).result(timeout=30)
        return (True, "") if content else (False, "模型返回空响应")
    except FutTimeout:
        return False, "模型探测超时（30 秒无响应）"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


def _live_rows(seeded: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    from evals.m3_loop_baseline import _wait_terminal
    from lawbuddy.agent.runner import AgentRunner
    from lawbuddy.agent.tasks import get_events
    from lawbuddy.citations import list_claims

    available, reason = _live_probe()
    out: list[dict[str, Any]] = [{
        "step": "live:probe", "status": "ok" if available else "skip", "reason": reason or None,
    }]
    if not available:
        out.append({"step": "live:note",
                    "status": "skip",
                    "reason": "未配置真实模型密钥或网关不可用：真实跑分与人工六维评分待配置后执行，不以 mock 替代"})
        return out

    for sc in SCENARIOS:
        entry = seeded[sc["key"]]
        task_id = entry["task"]["id"]
        AgentRunner.instance().start(task_id)
        final = _wait_terminal(task_id)
        events = get_events(task_id)
        turns = [e["payload"] for e in events if e["type"] == "turn_complete"]
        finalize = (turns[-1].get("finalize") or {}) if turns else {}
        depth = finalize.get("taskDepth")
        claims = list_claims(task_id=task_id)
        supported: set[str] = set()
        for c in claims:
            if c.get("status") != "supported":
                continue
            for src in str(c.get("sources") or "").split(","):
                src = src.strip()
                if src:
                    supported.add(src)
        gold_keys = {f"{entry['ids'][g['doc']]}:{g['page']}" for g in sc["golds"]}
        matched = sorted(gold_keys & supported)
        out.append({
            "step": f"live:{sc['key']}",
            "status": "ran",
            "lifecycle": final["lifecycle"],
            "depth": depth,
            "expectDepth": sc["expectDepth"],
            "depthMatch": depth == sc["expectDepth"],
            "goldTotal": len(gold_keys),
            "goldRecall": round(len(matched) / len(gold_keys), 3) if gold_keys else None,
            "matched": matched,
        })
    return out


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def run_eval(keep: bool = False, live: bool = False) -> dict[str, Any]:
    from lawbuddy.workspace import ensure_workspace

    tmp = Path(tempfile.mkdtemp(prefix="lawbuddy-eval-gold-"))
    report: dict[str, Any] = {
        "evalName": "m3_gold_standard",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "mode": "deterministic" + ("+live" if live else ""),
    }
    try:
        ensure_workspace(str(tmp / "ws"))
        seeded = _seed_all()
        report["scenarios"] = [
            {"key": sc["key"], "title": sc["title"], "caseId": seeded[sc["key"]]["case"]["id"],
             "golds": len(sc["golds"])}
            for sc in SCENARIOS
        ]
        rows, gates = _deterministic_rows(seeded)
        report["deterministic"] = {"ok": all(gates.values()), "rows": rows}
        if live:
            report["live"] = {"rows": _live_rows(seeded)}
        report["gates"] = gates
        report["passed"] = all(gates.values())
        report["notes"] = [
            "确定性门槛只证明金标集与机制层成立；实质语义质量仍需真实模型跑分与人工六维评分（§11.3）",
            "live 部分为报告项不设门槛：模型输出存在波动，评分标准由人工金标维护",
        ]
        report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
        return report
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def _print_summary(r: dict[str, Any]) -> None:
    print(f"\n=== M3 评测 · 实质法律质量金标集（{r['mode']}） ===")
    for sc in r.get("scenarios", []):
        print(f"  场景 {sc['key']}（{sc['title']}）：金标 {sc['golds']} 条")
    for row in r.get("deterministic", {}).get("rows", []):
        print(f"  ✓ {row['step']}: "
              f"{json.dumps({k: v for k, v in row.items() if k != 'step'}, ensure_ascii=False)}")
    for row in (r.get("live") or {}).get("rows", []):
        flag = "✓" if row.get("status") == "ran" else "·"
        print(f"  {flag} {row['step']}: "
              f"{json.dumps({k: v for k, v in row.items() if k != 'step'}, ensure_ascii=False)}")
    print("门槛判定：")
    for g, ok in r.get("gates", {}).items():
        print(f"  {'✓' if ok else '✗'} {g}")
    for n in r.get("notes", []):
        print(f"  · {n}")
    print(f"结论：{'金标集自检通过' if r.get('passed') else '金标集自检失败（见上方 ✗ 项）'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="实质法律质量金标集（§11.3）")
    parser.add_argument("--keep", action="store_true", help="保留评测工作空间（调试用）")
    parser.add_argument("--live", action="store_true",
                        help="附加真实模型跑分（需已配置模型密钥；未配置时显式 SKIP）")
    args = parser.parse_args()

    report = run_eval(keep=args.keep, live=args.live)
    _print_summary(report)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"m3_gold_standard_{ts}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存：{out}")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
