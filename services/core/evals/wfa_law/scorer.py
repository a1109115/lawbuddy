"""LAW 任务评分器（P2-12）。

原则：**不修改评分逻辑、不修改真值**（方案 §5.3）。官方 `verify.py` 可用时
优先调用官方实现；只有缺失时才用本包内的同语义实现兜底，并在结果里标注
`scorer: "builtin"`，使报告可区分"官方评分"与"本地口径"。
"""

from __future__ import annotations

import sys
from typing import Any

from .fixture import build_mini_fixture


def _load_official(reference_dir: Any) -> Any | None:
    if reference_dir is None:
        return None
    path = str(reference_dir)
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        import verify  # type: ignore

        return verify
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 本地兜底实现（与官方 verify.py 逐条同语义；仅在其不可用时使用）
# ---------------------------------------------------------------------------

def _parse(value: Any, multi: bool) -> Any:
    raw = str(value or "").strip()
    if multi:
        parts = [p.strip() for p in raw.split("|") if p.strip()]
        return frozenset(parts)
    return raw


def _cell_score(gold: Any, sub: Any, multi: bool) -> float:
    if sub is None:
        return 0.0
    if not multi:
        return 1.0 if gold == sub else 0.0
    if not gold and not sub:
        return 1.0
    tp = len(gold & sub)
    if tp == 0:
        return 0.0
    precision, recall = tp / len(sub), tp / len(gold)
    return 2 * precision * recall / (precision + recall)


def _norm_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return sorted({str(i).strip().upper() for i in value})


def _score_answer(q: dict[str, Any], gold: Any, sub: Any) -> tuple[float, float]:
    op = str(q.get("op") or "")
    if sub is None:
        return 0.0, 0.0
    if op in ("count", "n_profiles"):
        try:
            ok = int(sub) == gold and float(sub) == int(sub)
        except (TypeError, ValueError):
            ok = False
        return float(ok), float(ok)
    if op in ("list", "extremum", "minority", "outlier", "most_similar"):
        s = _norm_list(sub)
        if s is None:
            return 0.0, 0.0
        g, s = set(gold), set(s)
        if g == s:
            return 1.0, 1.0
        if not g or not s:
            return 0.0, 0.0
        tp = len(g & s)
        return 0.0, (0.0 if tp == 0 else 2 * tp / (len(g) + len(s)))
    if op == "distribution":
        if not isinstance(sub, dict):
            return 0.0, 0.0
        try:
            sub = {str(k).strip(): int(v) for k, v in sub.items()}
        except (TypeError, ValueError):
            return 0.0, 0.0
        extra = [k for k, v in sub.items() if k not in gold and v != 0]
        right = sum(1 for k in gold if sub.get(k, 0) == gold[k])
        exact = float(right == len(gold) and not extra)
        return exact, right / (len(gold) + len(extra))
    if op == "groups":
        if not isinstance(sub, list) or not all(isinstance(x, list) for x in sub):
            return 0.0, 0.0
        sg = {frozenset(_norm_list(x) or []) for x in sub if len(set(_norm_list(x) or [])) >= 2}
        gg = {frozenset(x) for x in gold}
        if sg == gg:
            return 1.0, 1.0

        def pairs(groups):
            return {frozenset((a, b)) for grp in groups for a in grp for b in grp if a < b}

        gp, sp = pairs(gg), pairs(sg)
        if not gp or not sp:
            return 0.0, 0.0
        tp = len(gp & sp)
        return 0.0, (0.0 if tp == 0 else 2 * tp / (len(gp) + len(sp)))
    raise ValueError(f"未知算子：{op}")


def score_submission(
    table: dict[tuple[str, str], Any],
    answers: dict[str, Any],
    fixture: dict[str, Any],
    *,
    reference_dir: Any = None,
) -> dict[str, Any]:
    """对一份提交打分：branch（逐格）+ merge（逐题精确）+ partial。

    table 键为 (agreement, deal_point)，值按交付点 multi 标记解析。
    """
    official = _load_official(reference_dir)
    multi = fixture["multi"]
    normalized = {
        (str(a).upper(), str(d)): _parse(v, bool(multi.get(d)))
        for (a, d), v in table.items()
        if d in fixture["options"] and str(a).upper() in fixture["agreements"]
    }
    gold_table = {
        (a, d): _parse(v, bool(multi.get(d))) for (a, d), v in fixture["table"].items()
    }
    per_dp: dict[str, list[float]] = {}
    for (a, d), g in gold_table.items():
        s = normalized.get((a, d))
        per_dp.setdefault(d, []).append(_cell_score(g, s, bool(multi.get(d))))
    cells = [x for v in per_dp.values() for x in v]
    branch = sum(cells) / len(cells) if cells else 0.0

    exact: list[float] = []
    partial: list[float] = []
    consistent: list[float] = []
    from lawbuddy import aggregate as _agg

    agg_table = {
        (a, d): _parse(v, bool(multi.get(d))) for (a, d), v in table.items()
    }
    for q in fixture["mergeSpec"]:
        qid = q["id"]
        gold = fixture["goldAnswers"].get(qid)
        sub = answers.get(qid)
        if official is not None:
            try:
                e, p = official.score_answer(q, gold, sub)
            except Exception:
                e, p = _score_answer(q, gold, sub)
        else:
            e, p = _score_answer(q, gold, sub)
        exact.append(float(e))
        partial.append(float(p))
        # consistency（官方 evaluate 同口径）：答案是否与该提交自己的表求值一致，
        # 用来发现「表 A 答 B」——branch 高但 consistency 低说明转写错位
        if qid in answers:
            try:
                own = _agg.check_consistency(q, agg_table, fixture["agreements"], sub)
                consistent.append(float(own["consistent"]))
            except Exception:
                consistent.append(0.0)
        else:
            consistent.append(0.0)
    merge = sum(exact) / len(exact) if exact else 0.0
    merge_partial = sum(partial) / len(partial) if partial else 0.0
    consistency = sum(consistent) / len(consistent) if consistent else 0.0
    thresholds = {"branch_min": 0.9, "merge_min": 0.75}
    return {
        "branch": round(branch, 4),
        "merge": round(merge, 4),
        "merge_partial": round(merge_partial, 4),
        "consistency": round(consistency, 4),
        "full_success": bool(branch >= thresholds["branch_min"] and merge >= thresholds["merge_min"]),
        "n_cells": len(cells),
        "n_merge_questions": len(exact),
        "scorer": "official" if official is not None else "builtin",
        "branch_by_deal_point": {d: round(sum(v) / len(v), 4) for d, v in sorted(per_dp.items())},
    }


def self_check() -> tuple[bool, dict[str, Any]]:
    """聚合口径自检：本地算子与真值表复算结果一致。"""
    from lawbuddy import aggregate

    fixture = build_mini_fixture()
    table, _warn = _table_to_aggregate(fixture)
    mismatches = 0
    detail: dict[str, Any] = {}
    for q in fixture["mergeSpec"]:
        try:
            value = aggregate.compute(q, table, fixture["agreements"])
        except Exception as exc:  # noqa: BLE001
            detail[q["id"]] = f"error: {type(exc).__name__}"
            mismatches += 1
            continue
        expect = fixture["goldAnswers"][q["id"]]
        if _norm(value) != _norm(expect):
            detail[q["id"]] = {"got": _norm(value), "expect": _norm(expect)}
            mismatches += 1
    return mismatches == 0, {"mismatch": mismatches, "detail": detail}


def _table_to_aggregate(fixture: dict[str, Any]) -> tuple[dict[tuple[str, str], Any], list[str]]:
    table: dict[tuple[str, str], Any] = {}
    for (a, d), v in fixture["table"].items():
        table[(a, d)] = _parse(v, bool(fixture["multi"].get(d)))
    return table, []


def _norm(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): int(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return sorted(str(v) for v in value)
    return value
