"""结构化表聚合（WFA-LAW 优化方案 §4.3 / P1-07）。

问题：跨文档聚合题（17 道 merge 题）此前靠模型"看着表自己算"，288 格 × 17 题的
统计在上下文里心算，merge 均值仅 0.074。oracle 实验证明这些算子在数学上完全
确定（官方 `verify.compute` 对同一张表复算与真值 0 处不一致）。

本模块把这些算子工具化：输入已落盘的表（CSV/JSON）+ 声明式问题描述，输出
确定值。实现与官方评分器 `verify.py` 的 `compute` / `eval_cond` / `_score`
逐语义对齐（纯标准库，离线可跑），使"工具算出来的答案"与"评分器认可的答案"
在同一口径上，避免表 A 答 B。

算子（9 种，与官方一致）：
  count / list / distribution / extremum / minority / outlier / groups /
  n_profiles / most_similar
条件（6 种）：all / any / not / eq / in / has / same_as
打分（2 种）：count_true / set_size
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from pathlib import Path
from typing import Any

SEP = " | "
MULTI_SEP_RE = "|"


class AggregateError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 表的读取与规范化
# ---------------------------------------------------------------------------

def _parse_answer(raw: Any, multi: bool) -> str | frozenset[str] | None:
    """与官方 parse_answer 同义：多选按 `|` 拆分取合法项集合，单选取原字符串。"""
    text = str(raw if raw is not None else "").strip()
    if multi:
        parts = [p.strip() for p in text.split(MULTI_SEP_RE) if p.strip()]
        return frozenset(parts)
    return text


def load_table(path: Path, *, agreements: list[str] | None = None,
               multi_points: set[str] | None = None) -> tuple[dict[tuple[str, str], Any], list[str]]:
    """读取 CSV 表 → {(agreement, deal_point): value}，返回（表, 警告）。

    列名大小写不敏感；缺少 agreement/deal_point/answer 任一列即报错（不猜列序）。
    同一单元格重复时后者覆盖（与官方一致）并给出警告。
    """
    if not path.exists():
        raise AggregateError(f"表文件不存在：{path.name}（请先把表落盘到工作目录）")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    header = [str(h or "").strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in ("agreement", "deal_point", "answer") if c not in header]
    if missing:
        raise AggregateError(
            f"表缺少必需列：{'、'.join(missing)}（需要 agreement / deal_point / answer）"
        )
    multi_points = {str(x) for x in (multi_points or set())}
    allowed_agr = {str(a).strip().upper() for a in agreements} if agreements else None
    table: dict[tuple[str, str], Any] = {}
    warnings: list[str] = []
    dup = 0
    for row in reader:
        norm = {(k or "").strip().lower(): v for k, v in row.items()}
        a = str(norm.get("agreement") or "").strip().upper()
        d = str(norm.get("deal_point") or "").strip()
        if not a or not d:
            continue
        if allowed_agr is not None and a not in allowed_agr:
            continue  # 不属于本题范围的协议：忽略而非报错（表可能覆盖全部任务）
        if (a, d) in table:
            dup += 1
        table[(a, d)] = _parse_answer(norm.get("answer"), d in multi_points)
    if dup:
        warnings.append(f"{dup} 个重复单元格（保留最后一行）")
    if not table:
        raise AggregateError("表中没有可用数据行（agreement / deal_point / answer 均为空）")
    return table, warnings


def load_json_table(path: Path) -> Any:
    """读取 JSON 表（answers.json / judgment.json 等按结构化对象处理）。"""
    if not path.exists():
        raise AggregateError(f"文件不存在：{path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        raise AggregateError(f"JSON 解析失败：{exc}") from exc


# ---------------------------------------------------------------------------
# 条件求值（与官方 eval_cond 逐条对应）
# ---------------------------------------------------------------------------

def _val(table: dict[tuple[str, str], Any], a: str, d: str) -> Any:
    return table.get((a, d))


def eval_cond(cond: Any, table: dict[tuple[str, str], Any], a: str) -> bool:
    if not isinstance(cond, dict):
        raise AggregateError("条件必须是对象（如 {\"dp\": \"mae\", \"eq\": \"Yes\"}）")
    if "all" in cond:
        return all(eval_cond(x, table, a) for x in cond["all"])
    if "any" in cond:
        return any(eval_cond(x, table, a) for x in cond["any"])
    if "not" in cond:
        return not eval_cond(cond["not"], table, a)
    dp = cond.get("dp")
    if not dp:
        raise AggregateError("条件缺少 dp（deal_point）字段")
    v = _val(table, a, str(dp))
    if v is None:
        return False
    if "eq" in cond:
        return v == cond["eq"]
    if "in" in cond:
        return v in cond["in"]
    if "has" in cond:
        return cond["has"] in v if isinstance(v, frozenset) else str(cond["has"]) == str(v)
    if "same_as" in cond:
        w = _val(table, a, str(cond["same_as"]))
        return w is not None and v == w
    raise AggregateError(f"不支持的条件：{json.dumps(cond, ensure_ascii=False)}")


def _key(v: Any) -> Any:
    return tuple(sorted(v)) if isinstance(v, frozenset) else v


def _profile(table: dict[tuple[str, str], Any], a: str, dps: list[str]) -> tuple:
    return tuple(_key(_val(table, a, d)) for d in dps)


def _modes(table: dict[tuple[str, str], Any], agrs: list[str], d: str) -> set:
    cnt = Counter(_key(_val(table, a, d)) for a in agrs)
    if not cnt:
        return set()
    top = max(cnt.values())
    return {k for k, n in cnt.items() if n == top}


def _score(spec: Any, table: dict[tuple[str, str], Any], a: str) -> float:
    if not isinstance(spec, dict):
        raise AggregateError("score 必须是对象（count_true / set_size）")
    t = spec.get("type")
    if t == "count_true":
        return sum(1 for c in spec.get("conds") or [] if eval_cond(c, table, a))
    if t == "set_size":
        v = _val(table, a, str(spec.get("dp"))) or frozenset()
        exclude = spec.get("exclude") or []
        return len([x for x in v if x not in exclude])
    raise AggregateError(f"不支持的 score 类型：{t}")


# ---------------------------------------------------------------------------
# 聚合算子（与官方 compute 逐算子对应）
# ---------------------------------------------------------------------------

OPS = ("count", "list", "distribution", "extremum", "minority", "outlier",
       "groups", "n_profiles", "most_similar")


def compute(question: dict[str, Any], table: dict[tuple[str, str], Any], agrs: list[str]) -> Any:
    """对一个问题机械求值（与官方 verify.compute 同语义，不依赖模型推理）。"""
    op = str(question.get("op") or "").strip()
    if op not in OPS:
        raise AggregateError(f"不支持的算子：{op}（可用：{'、'.join(OPS)}）")
    agrs = sorted({str(a).strip().upper() for a in agrs})
    if op == "count":
        return sum(1 for a in agrs if eval_cond(question["cond"], table, a))
    if op == "list":
        return [a for a in agrs if eval_cond(question["cond"], table, a)]
    if op == "distribution":
        dp = str(question["dp"])
        options = [str(o) for o in (question.get("options") or [])]
        out = {o: 0 for o in options}
        multi = bool(question.get("multi"))
        for a in agrs:
            v = _val(table, a, dp)
            if v is None:
                continue
            for o in (v if multi else [v]):
                out[str(o)] = out.get(str(o), 0) + 1
        return out
    if op == "extremum":
        sc = {a: _score(question["score"], table, a) for a in agrs}
        if not sc:
            return []
        pick = max if str(question.get("mode") or "max") == "max" else min
        best = pick(sc.values())
        return [a for a in agrs if sc[a] == best]
    if op == "minority":
        dp = str(question["dp"])
        limit = int(question.get("max_holders") or 0)
        cnt = Counter(_key(_val(table, a, dp)) for a in agrs)
        return [a for a in agrs if cnt[_key(_val(table, a, dp))] <= limit]
    if op == "outlier":
        dps = [str(d) for d in (question.get("dps") or [])]
        modes = {d: _modes(table, agrs, d) for d in dps}
        dev = {a: sum(1 for d in dps if _key(_val(table, a, d)) not in modes[d]) for a in agrs}
        min_dev = int(question.get("min_dev") or 1)
        return [a for a in agrs if dev[a] >= min_dev]
    if op == "groups":
        dps = [str(d) for d in (question.get("dps") or [])]
        g: dict[tuple, list[str]] = {}
        for a in agrs:
            g.setdefault(_profile(table, a, dps), []).append(a)
        return sorted(sorted(v) for v in g.values() if len(v) >= 2)
    if op == "n_profiles":
        dps = [str(d) for d in (question.get("dps") or [])]
        return len({_profile(table, a, dps) for a in agrs})
    # most_similar
    dps = [str(d) for d in (question.get("dps") or [])]
    target = str(question.get("target") or "").strip().upper()
    if target not in agrs:
        return None
    sim = {
        a: sum(
            1 for d in dps
            if _val(table, a, d) is not None and _val(table, a, d) == _val(table, target, d)
        )
        for a in agrs if a != target
    }
    if not sim:
        return []
    best = max(sim.values())
    return [a for a in sorted(sim) if sim[a] == best]


def check_consistency(
    question: dict[str, Any], table: dict[tuple[str, str], Any], agrs: list[str], answer: Any,
) -> dict[str, Any]:
    """校验"写进答案的值"是否与"表内容机械求值的结果"一致（§4.3 第 4 条）。

    防的是"表 A 答 B"：模型从表算出结果后，转写答案时手抄错、或用了另一份
    已过期的表。对不上的题返回 consistent=False 并同时给出两侧值，调用方据此
    要求重算或标为待核实，而不是把不一致的答案当成确定事实。
    """
    try:
        expect = compute(question, table, agrs)
    except (AggregateError, KeyError) as exc:
        return {
            "id": question.get("id"),
            "op": question.get("op"),
            "consistent": False,
            "error": f"无法求值：{exc}",
        }
    got = answer
    # 数值题：允许 "3" 与 3 的等价；列表题：忽略顺序；分布题：忽略零值差异
    if isinstance(expect, (int, float)) and not isinstance(expect, bool):
        try:
            ok = float(got) == float(expect) and float(got) == int(float(got))
        except (TypeError, ValueError):
            ok = False
    elif isinstance(expect, list):
        ok = _sorted(got) == _sorted(expect)
    elif isinstance(expect, dict):
        ok = isinstance(got, dict) and {
            str(k): int(v) for k, v in got.items() if str(v).strip() not in ("", "0")
        } == {str(k): int(v) for k, v in expect.items() if int(v) != 0}
    else:
        ok = got == expect
    return {
        "id": question.get("id"),
        "op": question.get("op"),
        "consistent": bool(ok),
        "expected": expect if not isinstance(expect, (frozenset, set)) else sorted(expect),
        "got": got,
    }


def _sorted(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return sorted(str(v).strip() for v in value)


def aggregate(payload: dict[str, Any], root: Path) -> dict[str, Any]:
    """工具入口：读表 → 逐题求值 → 返回确定值（含一致性自检信息）。

    payload:
      tablePath  相对工作空间的 CSV 路径（必填）
      agreements 参与聚合的协议列表（缺省 = 表中出现的全部）
      dealPoints 多选列声明 {"dpId": true}（distribution 需要 options 时一并给出）
      options    {"dpId": ["A","B",...]} 选项全集（distribution 用）
      questions  [{id, op, ...}] 聚合问题
    """
    raw_path = str(payload.get("tablePath") or "").strip()
    if not raw_path:
        raise AggregateError("tablePath 不能为空（请给出已落盘的 CSV 表路径）")
    rel = raw_path.replace("\\", "/")
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise AggregateError("表路径越界：只能读取工作空间内的文件")
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        raise AggregateError("questions 不能为空（至少一个聚合问题）")

    multi_map = payload.get("dealPoints") or {}
    multi_points = {str(k) for k, v in multi_map.items() if v} if isinstance(multi_map, dict) else set()
    agreements = payload.get("agreements")
    agreements = [str(a) for a in agreements] if isinstance(agreements, list) else None

    table, warnings = load_table(candidate, agreements=agreements, multi_points=multi_points)
    agrs = agreements or sorted({a for (a, _d) in table})
    option_map = payload.get("options") or {}

    results: list[dict[str, Any]] = []
    questions_by_id: dict[str, dict[str, Any]] = {}
    for q in questions:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id") or f"Q{len(results) + 1}")
        spec = dict(q)
        spec.setdefault("id", qid)
        questions_by_id[qid] = spec
        # distribution 的选项全集可由 options 声明补齐（缺省用表中出现过的值）
        if str(spec.get("op")) == "distribution" and not spec.get("options"):
            dp = str(spec.get("dp") or "")
            declared = option_map.get(dp) if isinstance(option_map, dict) else None
            if isinstance(declared, list) and declared:
                spec["options"] = [str(o) for o in declared]
        try:
            value = compute(spec, table, agrs)
            results.append({"id": qid, "op": spec.get("op"), "value": value})
        except AggregateError as exc:
            results.append({"id": qid, "op": spec.get("op"), "error": str(exc)})
        except KeyError as exc:
            results.append({"id": qid, "op": spec.get("op"), "error": f"缺少字段：{exc}"})

    # 一致性校验（§4.3 第 4 条）：answers 给定时逐题对拍"答案 vs 表求值"，
    # 防止表 A 答 B（手抄错、用旧表）。不一致项显式返回，不静默通过。
    consistency: list[dict[str, Any]] = []
    submitted = payload.get("answers")
    if isinstance(submitted, dict) and submitted:
        for qid, answer in submitted.items():
            spec = questions_by_id.get(str(qid))
            if spec is None:
                continue
            consistency.append(check_consistency(spec, table, agrs, answer))

    return {
        "tablePath": rel,
        "agreements": agrs,
        "cells": len(table),
        "multiPoints": sorted(multi_points),
        "results": results,
        "warnings": warnings,
        "consistency": consistency,
        "consistent": all(c["consistent"] for c in consistency) if consistency else None,
        "note": (
            "以上为按算子机械求值的结果，可直接作为聚合题答案写入 answers.json；"
            "数值均由表内容确定性推出，不要再自行心算或改写。"
            "若某题 error 提示缺字段，请补全该题声明后重算。"
            "把已写入的答案回传给 answers 参数可做一致性校验（防止表 A 答 B）。"
        ),
    }
