"""长输出分批与合并后 Schema 校验（WFA-LAW 优化方案 §4.7 / P2-10）。

三类保障（原方案 §4.7 第 1、3 条；第 2 条「断连续写」在 model_gateway 内）：

1. **分批规划**：按实体数（被告/当事人）与体量给出批数与每批范围，避免一次性
   生成 12 个被告导致单次输出过大被云端切断。
2. **结构化合并**：按主键合并各批 JSON 片段，冲突项显式暴露，不做自由文本拼接。
3. **合并后完整性校验**：检查声明的主键是否齐全、roles 与 charges 的键是否一致、
   数值区间与排序，产出可展示的 issue 列表（不通过也不编造，交由模型补或标待核实）。

本模块纯函数，无 IO、无第三方依赖，便于在工具层与评测层复用。
"""

from __future__ import annotations

import re
from typing import Any

# 单批最多实体数（对应"每批 ≤4 个实体"）；可通过任务声明 itemsPerBatch 覆盖
DEFAULT_ITEMS_PER_BATCH = 4
# 单批估算 token 上限（中文约 1 字 ≈ 1 token，留足结构化开销）
DEFAULT_BATCH_TOKENS = 4000
# 从请求里识别实体数量的模式（"12 名被告" / "8 defendants" / "共 9 人"）
_COUNT_PATTERNS = (
    re.compile(r"(\d{1,3})\s*(?:名|位|个)?\s*(?:共同)?(?:被告|被告人|嫌疑人|当事人|上诉人|被上诉人)"),
    re.compile(r"(\d{1,3})\s*(?:defendants?|suspects?|parties)", re.IGNORECASE),
)


def detect_entity_count(text: str) -> int | None:
    """从请求文本里识别实体数量（被告人数等）；识别不到返回 None。"""
    for pattern in _COUNT_PATTERNS:
        m = pattern.search(str(text or ""))
        if m:
            try:
                value = int(m.group(1))
            except ValueError:
                continue
            if 1 < value <= 200:
                return value
    return None


def plan_batches(
    total: int,
    *,
    items_per_batch: int = DEFAULT_ITEMS_PER_BATCH,
    max_tokens: int = DEFAULT_BATCH_TOKENS,
    tokens_per_item: int = 600,
) -> dict[str, Any]:
    """规划分批：返回批数与每批的序号区间（1-based，闭区间）。

    同时考虑实体数与单批 token 预算，取两者中更小的一批容量，避免"4 个大被告
    就超 4000 token"的情况。总数为 0 或负数时返回零批（调用方据此走单次输出）。
    """
    total = int(total or 0)
    if total <= 0:
        return {"total": 0, "itemsPerBatch": 0, "batches": [], "reason": "无需分批"}
    per = max(1, int(items_per_batch or DEFAULT_ITEMS_PER_BATCH))
    if tokens_per_item > 0 and max_tokens > 0:
        per = max(1, min(per, max_tokens // tokens_per_item))
    batches = []
    start = 1
    while start <= total:
        end = min(total, start + per - 1)
        batches.append({"index": len(batches) + 1, "start": start, "end": end, "count": end - start + 1})
        start = end + 1
    return {
        "total": total,
        "itemsPerBatch": per,
        "batches": batches,
        "reason": f"{total} 个实体分 {len(batches)} 批（每批 ≤{per} 个）",
    }


def _key_of(item: Any, key_field: str) -> str:
    if isinstance(item, dict):
        return str(item.get(key_field) or "").strip()
    return str(item or "").strip()


def merge_fragments(
    fragments: list[Any],
    key_field: str,
    *,
    path: list[str] | None = None,
) -> dict[str, Any]:
    """按主键合并各批片段，返回（合并后列表, 冲突项）。

    冲突（同一主键出现不同内容）不静默取后者：两方都保留在 conflicts 里，
    由调用方转 needs_human。path 用于从片段对象中取出数组（如 ["defendants"]）。
    """
    merged: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    skipped: list[Any] = []
    for frag in fragments:
        rows: Any = frag
        if isinstance(frag, dict) and path:
            node: Any = frag
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            rows = node
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            skipped.append(_short(frag))
            continue
        for item in rows:
            if not isinstance(item, dict):
                skipped.append(_short(item))
                continue
            ident = _key_of(item, key_field)
            if not ident:
                skipped.append(_short(item))
                continue
            existing = index.get(ident)
            if existing is None:
                index[ident] = item
                merged.append(item)
            elif existing != item:
                conflicts.append({"key": ident, "existing": existing, "incoming": item})
    return {
        "merged": merged,
        "count": len(merged),
        "keys": [str(_key_of(x, key_field)) for x in merged],
        "conflicts": conflicts,
        "skipped": skipped,
    }


def _short(value: Any, limit: int = 200) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def validate_merged(
    merged: list[dict[str, Any]],
    key_field: str,
    *,
    expected: list[str] | None = None,
    roles_field: str = "roles",
    charges_field: str = "charges",
) -> dict[str, Any]:
    """合并后的完整性校验（§4.7 第 3 条）。

    检查项：
    - 主键齐全：expected 给定时，缺失与多余都要报出（8/12 被告这类硬要求）；
    - 主键唯一：合并后不得重复；
    - roles 与 charges 键一致：同一实体的角色表与罪名表的键集合必须相同，
      不一致说明分批间口径漂移（R6 风险）；
    - 空结构：roles/charges 存在但为空，视为未填写而非"无"。
    返回 {ok, issues[], stats}；issues 只描述问题，不自行编造缺失内容。
    """
    issues: list[dict[str, Any]] = []
    keys = [_key_of(x, key_field) for x in merged]
    seen: dict[str, int] = {}
    for k in keys:
        seen[k] = seen.get(k, 0) + 1
    dup = sorted(k for k, n in seen.items() if n > 1)
    if dup:
        issues.append({"code": "DUPLICATE_KEY", "text": f"主键重复：{'、'.join(dup)}", "keys": dup})
    if expected:
        want = [str(x).strip() for x in expected if str(x or "").strip()]
        missing = [k for k in want if k not in seen]
        extra = [k for k in keys if k and k not in want]
        if missing:
            issues.append({
                "code": "MISSING_ENTITY",
                "text": f"缺少 {len(missing)} 个实体：{'、'.join(missing)}",
                "keys": missing,
                "instruction": "请补齐上述实体的完整节点后再合并；确无该实体则说明原因。",
            })
        if extra:
            issues.append({
                "code": "UNEXPECTED_ENTITY",
                "text": f"出现未在范围内声明的实体：{'、'.join(extra)}",
                "keys": extra,
            })

    roles_mismatch: list[dict[str, Any]] = []
    empty_nodes: list[str] = []
    for item in merged:
        ident = _key_of(item, key_field)
        roles = item.get(roles_field)
        charges = item.get(charges_field)
        has_roles = isinstance(roles, dict)
        has_charges = isinstance(charges, dict)
        if not has_roles and not has_charges:
            continue
        if has_roles != has_charges:
            roles_mismatch.append({
                "key": ident,
                "reason": f"只有 {'roles' if has_roles else 'charges'}，缺少另一侧",
            })
            continue
        if (has_roles and not roles) or (has_charges and not charges):
            empty_nodes.append(ident)
            continue
        if set(roles) != set(charges):
            roles_mismatch.append({
                "key": ident,
                "reason": "roles 与 charges 的键集合不一致",
                "onlyRoles": sorted(set(roles) - set(charges)),
                "onlyCharges": sorted(set(charges) - set(roles)),
            })
    if roles_mismatch:
        issues.append({
            "code": "ROLES_CHARGES_MISMATCH",
            "text": f"{len(roles_mismatch)} 个实体的角色与罪名口径不一致",
            "items": roles_mismatch,
            "instruction": "按同一实体口径统一 roles 与 charges 的键；无法确定时标为待核实，不要凭空补键。",
        })
    if empty_nodes:
        issues.append({
            "code": "EMPTY_ENTITY_NODE",
            "text": f"{len(empty_nodes)} 个实体的 roles/charges 为空：{'、'.join(empty_nodes[:10])}",
            "keys": empty_nodes,
        })

    return {
        "ok": not issues,
        "issues": issues,
        "stats": {"entities": len(merged), "duplicates": len(dup),
                  "missing": len(expected) - len(set(keys) & set(str(x) for x in expected)) if expected else 0},
    }


def batch_hint(total: int, *, per_batch: int = DEFAULT_ITEMS_PER_BATCH) -> str:
    """给模型的分批生成提示（只描述分批口径，不改变结论内容）。"""
    plan = plan_batches(total, items_per_batch=per_batch)
    if not plan["batches"]:
        return ""
    ranges = "、".join(f"第 {b['start']}–{b['end']} 个" for b in plan["batches"])
    return (
        f"【长输出分批】本次需输出 {total} 个实体，请分 {len(plan['batches'])} 批生成：{ranges}。"
        "每批生成后立即用 deliverable_patch 按主键回写（不要等全部写完再一次性输出）；"
        "分批之间保持字段口径一致（同一实体的角色与罪名键集合必须相同）；"
        "全部批次写完后如收到口径不一致提示，补齐后再次回写，不要重写整份文件。"
    )
