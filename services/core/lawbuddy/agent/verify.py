"""轻量引用校验（verify 节点，自主循环收尾用）。

对最终回答中三种引用形态逐一回查：《文档名》第N页 / 第N-M页（区间）/ 仅《文档名》：
- 文档标题能在当前案件内匹配（杜绝跨案件引用与虚构文档）；
- 页码在文档已解析页中存在（区间取起止页校验；仅《文档名》确认文档在案即可）；
- **具体引文**经 citations.judge_claim 校验：优先取引用后紧跟的「原文短引」，
  其次在结论文本中寻找与页面原文重合的片段；找不到具体引文即标待核实。

关键约束（05-阅卷Agent升级方案 §6.1）：不得再以“整页文本”作为 snippet 自校验——
整页包含整页恒为真，会把“页面存在”误判成“结论已由原文支持”。本模块只证明
引文确实出现在指定页面，语义是否足以支持结论仍由模型复核与人工金标判断。

产出状态：supported / needs_human（含 reason code）。校验只做标注（不重写、
不打回重做），未回查项由调用方在回答末尾追加待核实清单；结果同步落 artifacts
（chat 产物）并作为前端引用角标的数据源。
仅《文档名》且未匹配到案件文档的引用（多为法条/书名）不计入待核实，避免噪音。
"""

from __future__ import annotations

import re
from typing import Any

# 引用形态：《文档名》[第N页 | 第N-M页 | 无页码]（system prompt 统一格式 + 常见变体）
_CITE_RE = re.compile(r"《([^《》\n]{1,80})》(?:\s*第\s*(\d{1,4})(?:\s*[-—~至]\s*(\d{1,4}))?\s*页)?")

# 引用后紧跟的逐字引文：：/：/，/空格 + 引号包裹（可带「原文/载明/记载」等引导词）
_QUOTE_TAIL_RE = re.compile(
    r"^[\s：:，,。；;（(]*(?:原文|载明|记载|内容为|表述为)?[\s：:，,]*"
    r"(?:「([^」\n]{2,300})」|“([^”\n]{2,300})”|\"([^\"\n]{2,300})\"|『([^』\n]{2,300})』)"
)

# 单文档页文本缓存上限（大卷宗逐文档懒加载）
_MAX_DOC_PAGE_LOOKUPS = 30

# 可接受的最短逐字引文（规范化后字符数）：过短片段（如「原告」）不构成证据
MIN_QUOTE_CHARS = 6

# 系统替模型补定位页面时要求的更强逐字重合（规范化后字符数）：仅凭 6 个通用字
# 命中就断定「该结论出自此页」过于草率，需更长的原文片段才能反推页码。
MIN_LOCATE_CHARS = 12

_QUOTE_SPLIT_RE = re.compile(r"[，。；、,.;:：!？?\s\n\r（）()\[\]【】]+")

# 材料清单式提及（§6.1）：一句/一行内并列出现多个《文档名》，且除文档名外只剩
# 标点、连接词与「卷宗材料包括」这类引导语时，这段文字是在罗列卷宗构成，不是在
# 断言某条事实出自某页原文。逐条计入待核实只会用噪音淹没真正的质量问题。
_CATALOG_ASSERTION_RE = re.compile(
    r"认定|记载|载明|表明|显示|证明|证实|构成|违反|矛盾|主张|陈述|供述|辩称|"
    r"判决|裁定|建议|意见|要求|约定|支付|交付|签署|签名|盖章|送达|通知|拒绝"
)
_CATALOG_FILLER_RE = re.compile(r"[\s\u3000，。；、,.;：:!？?（）()\[\]【】\-—_/～~·|*>#＝=“”\"'‘’]+")
_CATALOG_RESIDUE_MAX = 20


def _is_pure_catalog_line(line: str) -> bool:
    """整行只由《文档名》与列表标点构成（不含任何断言性文字）。

    用于识别「- 《判决书》\\n- 《起诉书》」这类逐行罗列的材料清单。
    """
    matches = list(_CITE_RE.finditer(line))
    if not matches or any(m.group(2) for m in matches):
        return False
    residue = _CATALOG_FILLER_RE.sub("", _CITE_RE.sub("", line))
    return not residue or (len(residue) <= 6 and not _CATALOG_ASSERTION_RE.search(residue))


def _is_catalog_mention(answer: str, start: int, end: int) -> bool:
    """该处《文档名》是否属于材料清单式提及（不构成事实引证）。

    只对「无页码」的文档名生效；带页码的引用一律按正式引用校验。
    """
    line_start = answer.rfind("\n", 0, start) + 1
    line_end = answer.find("\n", start)
    line = answer[line_start:(len(answer) if line_end < 0 else line_end)]
    matches = list(_CITE_RE.finditer(line))
    if not matches or any(m.group(2) for m in matches):
        return False
    if len(matches) >= 2:
        residue = _CATALOG_FILLER_RE.sub("", _CITE_RE.sub("", line))
        return len(residue) <= _CATALOG_RESIDUE_MAX and not _CATALOG_ASSERTION_RE.search(residue)
    if not _is_pure_catalog_line(line):
        return False
    # 单个文档名独占一行时，需与相邻同类行连成清单才算提及，避免放过单条断言
    lines = answer.split("\n")
    idx = answer[:start].count("\n")
    run = 1
    i = idx - 1
    while i >= 0 and _is_pure_catalog_line(lines[i]):
        run += 1
        i -= 1
    i = idx + 1
    while i < len(lines) and _is_pure_catalog_line(lines[i]):
        run += 1
        i += 1
    return run >= 2


def _match_doc(docs: list[dict[str, Any]], title: str) -> dict[str, Any] | None:
    """文档标题匹配：全等 → 包含（双向），命中多个取标题最短者。"""
    candidates = [d for d in docs if str(d.get("title") or "").strip() == title]
    if not candidates:
        candidates = [
            d for d in docs
            if title and (title in str(d.get("title") or "") or str(d.get("title") or "") in title)
        ]
    if not candidates:
        return None
    return min(candidates, key=lambda d: len(str(d.get("title") or "")))


def _explicit_quote(answer: str, pos: int) -> str:
    """取引用标记后紧跟的逐字引文（没有则返回空串）。"""
    m = _QUOTE_TAIL_RE.match(answer[pos:pos + 420])
    if not m:
        return ""
    for group in m.groups():
        if group and len(group.strip()) >= 2:
            return group.strip()
    return ""


def _overlap_quote(claim_text: str, page_text: str) -> str:
    """在结论文本中找与页面原文重合的最长分句（引文未显式标注时的降级定位）。

    只做“分句是否原样出现在该页”的确定性判断：命中即说明结论复述了原文片段，
    可交给 verify_quote 校验；未命中说明模型在转述或未给引文，交由人工核实。
    """
    from ..citations import normalize_text

    page_norm = normalize_text(page_text)
    if not page_norm:
        return ""
    chunks = [
        c.strip() for c in _QUOTE_SPLIT_RE.split(claim_text or "")
        if len(normalize_text(c)) >= MIN_QUOTE_CHARS
    ]
    chunks.sort(key=lambda c: len(normalize_text(c)), reverse=True)
    for chunk in chunks:
        if normalize_text(chunk) in page_norm:
            return chunk
    return ""


def _same_paragraph_before(answer: str, pos: int, limit: int = 200) -> str:
    """取引用标记之前、同一段落内的文字（遇空行或列表项即止）。

    漏写页码时用于反查原文所在页：只用同段文字，避免把上一条无关结论的句子
    拿来匹配，从而把页码补定到错误页面。
    """
    head = (answer or "")[:pos]
    cut = max(head.rfind("\n\n"), head.rfind("\r\n\r\n"))
    if cut >= 0:
        head = head[cut:]
    lines = head.split("\n")
    # 从当前行往前收集，遇到以列表符/标题符开头的行即视为另一条内容
    collected: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if collected and re.match(r"^(?:[-*+]|\d+[.、)]|#{1,6}\s)", stripped):
            break
        collected.append(stripped)
    return "\n".join(reversed(collected))[-limit:].strip()


def _locate_page(claim_text: str, pages: list[dict[str, Any]]) -> tuple[int, str]:
    """仅给出《文档名》时，在该文档内反查结论所复述的原文落在哪一页。

    只认逐字重合（规范化后），且该片段必须在文档内唯一出现——多页同时命中说明
    无法判断结论对应哪一页，此时不猜，返回 (0, "") 交由人工核实。
    """
    from ..citations import normalize_text

    chunks = [
        c.strip() for c in _QUOTE_SPLIT_RE.split(claim_text or "")
        if len(normalize_text(c)) >= MIN_LOCATE_CHARS
    ]
    chunks.sort(key=lambda c: len(normalize_text(c)), reverse=True)
    if not chunks:
        return 0, ""
    norm_pages = {
        int(p.get("pageNo") or 0): normalize_text(str(p.get("text") or ""))
        for p in pages
    }
    for chunk in chunks:
        needle = normalize_text(chunk)
        hits = [no for no, text in norm_pages.items() if needle in text]
        if len(hits) == 1:
            return hits[0], chunk
    return 0, ""


def verify_answer(task_id: str, case_id: str, answer: str) -> dict[str, Any]:
    """校验回答中的原文引用，返回 {total, supported, pending, cites}。

    - cites 携带 documentId/pageNo/snippet（页面片段预览）与 claimStatus，
      供前端角标悬浮卡直接展示，无需再发 RPC；未匹配文档的角标由前端标灰。
    - pending 每项为人类可读说明，包含未通过原因（引文未定位/引文不匹配/低 OCR 等）。
    - 校验异常不抛出（失败返回空结果），不阻断任务收束。
    """
    result: dict[str, Any] = {
        "total": 0, "supported": 0, "pending": [], "cites": [], "mentions": [],
    }
    try:
        from .. import documents as docs
        from ..citations import create_claim, judge_claim

        all_docs = docs.list_task_documents(case_id)
        pages_cache: dict[str, list[dict[str, Any]]] = {}
        seen: set[tuple[str, int]] = set()
        matches = list(_CITE_RE.finditer(answer or ""))
        result["total"] = len(matches)
        lookups = 0
        catalog_mentions = 0

        def _load_pages(doc: dict[str, Any], title: str, page_no: int) -> list[dict[str, Any]] | None:
            """懒加载文档页文本；超上限时按需记待核实并返回 None。"""
            nonlocal lookups
            if doc["id"] in pages_cache:
                return pages_cache[doc["id"]]
            if lookups >= _MAX_DOC_PAGE_LOOKUPS:
                if page_no:
                    result["pending"].append(f"《{title}》第{page_no}页：文档数超上限，未校验")
                return None
            lookups += 1
            pages_cache[doc["id"]] = docs.get_pages(doc["id"])
            return pages_cache[doc["id"]]

        for m in matches:
            title = m.group(1).strip()
            page_raw = m.group(2)
            end_raw = m.group(3)
            page_no = int(page_raw) if page_raw else 0  # 0 = 仅《文档名》整文档级引用
            key = (title, page_no)
            if key in seen:
                continue  # 同一出处只建一条 Claim
            doc = _match_doc(all_docs, title)
            if doc is None:
                seen.add(key)
                # 带页码却找不到文档 → 待核实；无页码引用多为法条/书名，静默跳过避免噪音
                if page_no:
                    result["pending"].append(f"《{title}》第{page_no}页：未在案件文档中找到该文档")
                continue
            if page_no == 0 and _is_catalog_mention(answer or "", m.start(), m.end()):
                # 材料清单式提及：只登记为「提到了这份材料」，不建 Claim、不计待核实，
                # 也不占用该文档名的 seen 槽位（后文可能还有真正的引证）。
                catalog_mentions += 1
                if not any(x["documentId"] == doc["id"] for x in result["mentions"]):
                    result["mentions"].append({
                        "title": str(doc.get("title") or title),
                        "documentId": doc["id"],
                    })
                continue
            seen.add(key)
            pages = _load_pages(doc, title, page_no)
            if pages is None:
                continue
            claim_text = (answer or "")[max(0, m.start() - 120):m.start()].strip()
            located_quote = ""
            if page_no == 0:
                # 仅《文档名》：先在文档内确定性反查结论复述的原文页；定位不到才标待核实，
                # 避免“模型写了文档名但漏页码”被整批计为无法回查（不猜、不编页码）。
                located_no, located_quote = _locate_page(
                    _same_paragraph_before(answer, m.start()), pages
                )
                if not located_no:
                    cite = create_claim(
                        task_id, case_id, "fact", (claim_text or f"《{title}》")[:200],
                        "needs_human", "PAGE_NOT_CITED", None, None,
                    )
                    result["pending"].append(
                        f"《{title}》：未给出页码，且结论文字与该文档原文无唯一对应的逐字片段，待人工核实"
                    )
                    result["cites"].append({
                        "title": str(doc.get("title") or title),
                        "documentId": doc["id"],
                        "claimId": cite.get("claimId"),
                        "claimStatus": cite.get("claimStatus"),
                        "reasonCode": cite.get("reasonCode"),
                    })
                    continue
                page_no = located_no
                m = None  # 页码由系统补定，下方 quote 直接用定位到的片段
            page = next((p for p in pages if int(p.get("pageNo") or 0) == page_no), None)
            if page is None:
                result["pending"].append(f"《{title}》第{page_no}页：页码超出文档范围（共 {len(pages)} 页）")
                continue
            cite_row: dict[str, Any] = {
                "title": str(doc.get("title") or title),
                "pageNo": page_no,
                "documentId": doc["id"],
            }
            if located_quote:
                # 页码由系统依据逐字唯一命中补定，标注出来便于人工复核
                cite_row["pageLocatedBySystem"] = True
            if end_raw:
                end_no = int(end_raw)
                if end_no < page_no or end_no > len(pages):
                    result["pending"].append(
                        f"《{title}》第{page_no}-{end_no}页：页码区间超出文档范围（共 {len(pages)} 页）"
                    )
                    continue
                cite_row["pageEnd"] = end_no
            page_text = str(page.get("text") or "")
            quote = located_quote or (
                (_explicit_quote(answer, m.end()) or _overlap_quote(claim_text, page_text))
                if m is not None else ""
            )
            if not quote:
                cite = create_claim(
                    task_id, case_id, "fact",
                    (claim_text or f"《{title}》第{page_no}页")[:200],
                    "needs_human", "QUOTE_NOT_LOCATABLE", None, None,
                )
                result["pending"].append(
                    f"《{title}》第{page_no}页：结论未给出可定位的原文短引，待人工核实"
                )
                cite_row["claimId"] = cite.get("claimId")
                cite_row["claimStatus"] = cite.get("claimStatus")
                cite_row["reasonCode"] = cite.get("reasonCode")
                cite_row["snippet"] = page_text[:120]
                result["cites"].append(cite_row)
                continue
            hit = {
                "documentId": doc["id"],
                "versionId": doc.get("activeVersionId"),
                "pageNo": page_no,
                "snippet": quote,
            }
            cite = judge_claim(
                task_id=task_id,
                case_id=case_id,
                text=(claim_text or f"《{title}》第{page_no}页")[:200],
                source_hit=hit,
            )
            status = str(cite.get("claimStatus") or "")
            reason = str(cite.get("reasonCode") or "")
            if status == "supported":
                result["supported"] += 1
            else:
                label = {
                    "QUOTE_MISMATCH": "引文与该页原文不匹配",
                    "LOW_OCR_CONFIDENCE": "该页 OCR 置信度偏低",
                }.get(reason, "无法确认原文支持")
                result["pending"].append(
                    f"《{title}》第{page_no}页：{label}（引文「{quote[:30]}」），待人工核实"
                )
            cite_row["claimId"] = cite.get("claimId")
            cite_row["claimStatus"] = cite.get("claimStatus")
            cite_row["reasonCode"] = cite.get("reasonCode")
            cite_row["quote"] = quote[:120]
            cite_row["snippet"] = page_text[:120]
            result["cites"].append(cite_row)
    except Exception:  # noqa: BLE001  校验失败不阻断任务收束
        return {"total": 0, "supported": 0, "pending": [], "cites": [], "mentions": []}
    result["total"] = max(0, result["total"] - catalog_mentions)
    return result
