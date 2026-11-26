"""同族条款角色消歧（WFA-LAW 优化方案 §4.5 / P1-08）。

问题：并购类合同里 Target/Company 与 Parent/Buyer 的 MAE 定义常常相邻出现。
检索只能保证"找到 MAE 定义"，不区分定义主体；模型选中相邻的 Parent 侧定义
作答，导致目标方 18 格整体错位（A05 曾 0.22、A16 仅 0.11）。

本模块做两件事（都不改写原文，只做识别与提示）：
1. 扫描合同全文，把同一中心术语的各"前缀变体"归为一个族，并标注每个变体
   指向的主体方（Parent/Buyer → 收购方；Company/Target/Seller → 目标方）；
2. 校验一条抽取结果：若引用的定义变体主体与题目要求的主体不一致，返回
   `mismatch`，调用方据此标为 needs_human，而不是静默给出确定答案。

前缀到主体的映射是确定性规则，只用于提示与拦截；术语是否确实采用该定义，
仍需模型结合原文语义判断。
"""

from __future__ import annotations

import re
from typing import Any

# 定义句：`"Term" means ...` / `“Term”是指 ...` / `Term 系指 ...`
_DEF_PATTERNS = (
    re.compile(r"[\"“]([^\"”]{2,60})[\"”]\s*(?:means|shall mean|refers to)\b", re.IGNORECASE),
    re.compile(r"[\"“]([^\"”]{2,60})[\"”][，,]?\s*(?:系指|是指|指的是)"),
    re.compile(r"(?<![\w\u4e00-\u9fa5])([A-Z][A-Za-z]{1,20}(?:\s+[A-Z][A-Za-z]{1,20}){0,4})\s+(?:means|shall mean)\b"),
)

# 主体前缀 → 角色。顺序有意义：先匹配更长/更具体的前缀。
_OWNER_PREFIXES: tuple[tuple[str, str, str], ...] = (
    ("parent", "acquirer", "收购方/控股方（Parent）"),
    ("buyer", "acquirer", "收购方/控股方（Buyer）"),
    ("acquirer", "acquirer", "收购方（Acquirer）"),
    ("purchaser", "acquirer", "收购方（Purchaser）"),
    ("company", "target", "目标公司（Company）"),
    ("target", "target", "目标公司（Target）"),
    ("seller", "target", "出售方/目标方（Seller）"),
    ("vendor", "target", "出售方/目标方（Vendor）"),
)

# 题目常用主体词 → 期望角色
_SUBJECT_ROLE_HINTS: dict[str, str] = {
    "target": "target", "company": "target", "目标": "target", "目标方": "target",
    "目标公司": "target", "卖方": "target", "被收购方": "target", "seller": "target",
    "parent": "acquirer", "buyer": "acquirer", "acquirer": "acquirer",
    "收购方": "acquirer", "买方": "acquirer", "控股方": "acquirer",
}

# 默认关注的定义族中心术语（MAE 是最典型的对称定义；可按需扩展）
DEFAULT_HEADS = ("material adverse effect", "material adverse change")


def subject_role(subject: str) -> str | None:
    """把题目主体（中英文）映射到 target / acquirer；无法识别返回 None。"""
    text = str(subject or "").strip().lower()
    if not text:
        return None
    if text in _SUBJECT_ROLE_HINTS:
        return _SUBJECT_ROLE_HINTS[text]
    for key, role in _SUBJECT_ROLE_HINTS.items():
        if key in text:
            return role
    return None


def _split_owner(term: str) -> tuple[str | None, str, str]:
    """拆分术语前缀：'Parent Material Adverse Effect' → (acquirer, Parent, 中心术语)。"""
    cleaned = re.sub(r"\s+", " ", str(term or "").strip())
    lower = cleaned.lower()
    for prefix, role, label in _OWNER_PREFIXES:
        if lower.startswith(prefix + " "):
            rest = cleaned[len(prefix):].strip()
            return role, prefix.capitalize(), rest
    return None, "", cleaned


def _find_definitions(text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pat in _DEF_PATTERNS:
        for m in pat.finditer(text):
            term = re.sub(r"\s+", " ", m.group(1)).strip()
            if not term or term.lower() in seen:
                continue
            seen.add(term.lower())
            found.append({"term": term, "offset": m.start()})
    return found


def families(text: str, heads: tuple[str, ...] = DEFAULT_HEADS,
             page_no: int | None = None) -> list[dict[str, Any]]:
    """把同中心术语的前缀变体归族：每个族列出全部变体及其主体方。

    `page_no` 仅用于定位记录（audit_document 逐页扫描时写入），不影响归族结果。
    """
    defs = _find_definitions(text)
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in defs:
        role, prefix, head = _split_owner(item["term"])
        key = head.lower().strip()
        if not key:
            continue
        if heads and not any(h in key for h in heads):
            continue
        buckets.setdefault(key, []).append({
            "term": item["term"],
            "prefix": prefix or None,
            "role": role,
            "offset": item["offset"],
            "pageNo": page_no,
        })
    out: list[dict[str, Any]] = []
    for key, variants in buckets.items():
        if len(variants) < 2:
            continue  # 单一定义不存在"两侧混淆"风险
        out.append({
            "head": key,
            "variants": sorted(variants, key=lambda v: v["offset"]),
            "symmetric": any(v["role"] == "target" for v in variants)
            and any(v["role"] == "acquirer" for v in variants),
        })
    return out


def audit(quote: str, subject: str, text: str) -> dict[str, Any]:
    """校验抽取结果引用的定义变体是否与题目主体一致。

    返回 verdict：
      aligned   引用变体与题目主体同侧（或该术语无前缀）
      mismatch  引用了另一侧的定义（应标 needs_human，不得作为确定答案）
      unknown   引文中找不到任何族内术语，或题目主体无法识别
    """
    want = subject_role(subject)
    fams = families(text)
    q = str(quote or "")
    used: dict[str, Any] | None = None
    for fam in fams:
        for v in fam["variants"]:
            if v["term"] and v["term"].lower() in q.lower():
                used = {**v, "head": fam["head"]}
                break
        if used:
            break
    result: dict[str, Any] = {
        "subject": subject,
        "subjectRole": want,
        "families": [
            {
                "head": f["head"],
                "symmetric": f["symmetric"],
                "variants": [
                    {"term": v["term"], "prefix": v["prefix"], "role": v["role"]} for v in f["variants"]
                ],
            }
            for f in fams
        ],
    }
    if used is None:
        result.update({"verdict": "unknown", "matchedTerm": None,
                       "hint": "引文中未出现本清单内的同族术语；若题目确需该定义，请先检索定义条款原文。"})
        return result
    result.update({"matchedTerm": used["term"], "matchedRole": used["role"]})
    if want is None:
        result.update({"verdict": "unknown", "hint": "无法识别题目主体，请明确问的是目标方还是收购方。"})
        return result
    if used["role"] is None:
        result.update({"verdict": "aligned", "hint": "引用的是无前缀的通用定义，未与其他主体定义混淆。"})
        return result
    if used["role"] == want:
        result.update({"verdict": "aligned", "hint": "引用定义的主体与题目主体一致。"})
        return result
    result.update({
        "verdict": "mismatch",
        "hint": (
            f"引用的「{used['term']}」是另一侧主体的定义，而题目问的是 {subject}。"
            "请改用同族中对应主体的定义作答；两侧定义都找不到时把该格标为待核实（needs_human），"
            "不得用另一侧定义代替。"
        ),
    })
    return result


def document_text(document_id: str) -> str:
    """取文档全文（按页拼接），供族扫描使用；不返回给模型，只用于本模块判定。"""
    from . import documents as docs

    pages = docs.get_pages(document_id)
    return "\n".join(str(p.get("text") or "") for p in pages)


def audit_document(document_id: str, quote: str, subject: str) -> dict[str, Any]:
    from . import documents as docs

    pages = docs.get_pages(document_id)
    text = "\n".join(str(p.get("text") or "") for p in pages)
    result = audit(quote, subject, text)
    # 定义类条款定位记录（B-02 §4.7）：逐页扫描同族定义，返回术语与页码，
    # 供模型回到原文核对；只记录术语/角色/页码，不返回整页正文。
    located: dict[str, dict[str, Any]] = {}
    for page in pages:
        page_no = int(page.get("pageNo") or 0)
        for family in families(str(page.get("text") or ""), page_no=page_no):
            for variant in family["variants"]:
                key = f"{str(variant['term']).lower()}|{page_no}"
                located[key] = {
                    "term": variant["term"],
                    "role": variant["role"],
                    "pageNo": page_no,
                }
    result["definitionLocations"] = sorted(
        located.values(), key=lambda item: (item["pageNo"], item["term"]))[:50]
    result["documentId"] = document_id
    return result
