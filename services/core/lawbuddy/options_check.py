"""多选题逐项核对（WFA-LAW 优化方案 §4.6 / P2-09）。

问题：多选格（如 `mae_co_economic`）要求选出"所有适用情形"，模型倾向把
"相关"当成"包含"，把原文没写明的选项也勾上——基线多选 F1 仅 0.69。

本模块把核对做成两段式并强制留痕：
  候选 → 逐项核对（每项必须有原文引句 + 页码）→ 仅保留有证据的项
无证据的候选不会被硬选，而是进入 `needs_human` 列表，由调用方标注待核实。
引句通过 `citations.verify_quote` 回查原文，回查失败的项同样不保留。
"""

from __future__ import annotations

from typing import Any

STATUS_SUPPORTED = "supported"
STATUS_NEEDS_HUMAN = "needs_human"


def _verify(quote: str, document_id: str, page_no: int) -> bool:
    from .citations import normalize_text, verify_quote
    from .storage import get_db

    try:
        row = get_db().query_one(
            "SELECT active_version_id FROM documents WHERE id = ?", (document_id,)
        )
        if row is None or not row["active_version_id"]:
            return False
        return verify_quote(normalize_text(quote), str(row["active_version_id"]), int(page_no))
    except Exception:
        # 库未初始化 / 版本失效 / 页码越界一律按"无法回查"处理：
        # 核对工具只在能确证原文时给出 supported，绝不因异常放宽标准
        return False


def check(payload: dict[str, Any]) -> dict[str, Any]:
    """逐项核对多选候选。

    payload:
      options   候选选项列表（字符串）或对象列表 [{value, quote, documentId, pageNo}]
      subject   这一格问的是什么（用于回报，不参与判定）
    """
    raw = payload.get("options")
    if not isinstance(raw, list) or not raw:
        raise ValueError("options 不能为空（先列出候选集合，再逐项核对）")
    subject = str(payload.get("subject") or "").strip()
    kept: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for item in raw:
        if isinstance(item, str):
            pending.append({"value": item, "reason": "未提供原文引句，无法核对"})
            continue
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or item.get("option") or "").strip()
        if not value:
            continue
        quote = str(item.get("quote") or "").strip()
        doc_id = str(item.get("documentId") or "").strip()
        page = item.get("pageNo")
        if not quote or not doc_id or page in (None, ""):
            pending.append({"value": value, "reason": "缺少 quote / documentId / pageNo"})
            continue
        try:
            page_no = int(page)
        except (TypeError, ValueError):
            pending.append({"value": value, "reason": "页码无效"})
            continue
        if not _verify(quote, doc_id, page_no):
            errors.append({"value": value, "reason": "引句与原文不匹配（引文须逐字摘录原文）"})
            continue
        kept.append({
            "value": value,
            "quote": quote,
            "documentId": doc_id,
            "pageNo": page_no,
            "status": STATUS_SUPPORTED,
        })

    return {
        "subject": subject,
        "selected": [k["value"] for k in kept],
        "supported": kept,
        "needsHuman": pending,
        "invalidQuotes": errors,
        "allVerified": not pending and not errors,
        "guidance": (
            "只有 supported 项可以写入答案。needsHuman 项在回答中标注待核实，"
            "不得作为已选项；invalidQuotes 项需重新定位原文后复核。"
            if pending or errors else
            "全部候选均有可回查的原文证据，可按 selected 写入答案。"
        ),
    }
