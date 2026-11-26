"""M4 优化项评测（WFA-LAW 优化方案 P0-04 / P1-07 / P1-08 / P2-09）。

覆盖四个可确定性验证的能力，不需要真实模型：

- P0-04 交付物骨架与周期回写：任务启动即落盘可解析骨架；deliverable_patch
  按主键 upsert 不丢内容；在 30% / 60% / 90% 轮数处中断后交付物仍存在且可解析；
  收尾模式判定在预算将尽时生效并阻断新检索。
- P1-07 聚合工具：与官方 verify.py 的 compute/eval_cond/_score 对拍；对同一张
  表 17 道聚合题零不一致。
- P1-08 条款角色消歧：同族定义枚举、主体错配判为 mismatch、主体一致判为 aligned。
- P2-09 多选逐项核对：无引句/引句不匹配的候选不得选中，只有可回查项保留。
- P2-10 长输出分批：实体数识别、分批规划、按主键合并、roles/charges 口径校验。
- P2-11 blob 生命周期：只读 blob 可被删除；DEK 不匹配被明确识别且不降级明文；
  工作空间删除→重建→重新导入后全部 ready。
- P1-06 原件区隔离补充：submission/ 可写、迁移期警告模式放行并留审计。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any


def _report(name: str, ok: bool, **extra: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), **extra}


# ---------------------------------------------------------------------------
# P0-04 交付物骨架与周期回写
# ---------------------------------------------------------------------------

def _run_deliverables(work: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy.agent import deliverables as store

    rows: list[dict[str, Any]] = []
    root = work / "ws-deliverables"
    (root / "submission").mkdir(parents=True, exist_ok=True)

    specs = store.normalize_specs([
        {"name": "deal_points.csv",
         "columns": ["agreement", "deal_point", "answer"],
         "key": ["agreement", "deal_point"]},
        {"name": "answers.json", "skeleton": {}, "path": ["answers"], "itemKey": "id"},
        {"name": "report.md", "title": "分析报告"},
    ])
    rows.append(_report("specsNormalized", len(specs) == 3))

    states = store.ensure_skeleton(root, specs)
    rows.append(_report("skeletonCreated", all(s.get("created") for s in states),
                        files=[s["name"] for s in states]))
    # 骨架必须可解析：CSV 有表头、JSON 可 loads、MD 有标题
    csv_head = (root / "submission/deal_points.csv").read_text(encoding="utf-8").strip()
    rows.append(_report("csvHeaderParsable", csv_head == "agreement,deal_point,answer"))
    json.loads((root / "submission/answers.json").read_text(encoding="utf-8"))
    rows.append(_report("jsonSkeletonParsable", True))

    # 幂等：重复 ensure 不覆盖已回写内容
    store.patch(root, specs, "deal_points.csv",
                {"rows": [{"agreement": "A01", "deal_point": "mae", "answer": "Yes"}]})
    store.ensure_skeleton(root, specs)
    body = (root / "submission/deal_points.csv").read_text(encoding="utf-8")
    rows.append(_report("ensureIsIdempotent", "A01" in body))

    # 按主键 upsert：同一主键更新而非追加
    store.patch(root, specs, "deal_points.csv",
                {"rows": [{"agreement": "A01", "deal_point": "mae", "answer": "No"}]})
    lines = [ln for ln in (root / "submission/deal_points.csv").read_text(encoding="utf-8").splitlines() if ln]
    rows.append(_report("upsertNotAppend", len(lines) == 2 and "No" in lines[1], lines=lines))

    # 第二个主键追加
    store.patch(root, specs, "deal_points.csv",
                {"rows": [{"agreement": "A02", "deal_point": "mae", "answer": "Yes"}]})
    lines2 = [ln for ln in (root / "submission/deal_points.csv").read_text(encoding="utf-8").splitlines() if ln]
    rows.append(_report("appendNewKey", len(lines2) == 3))

    # JSON 数组按 itemKey upsert
    store.patch(root, specs, "answers.json", {"rows": [{"id": "Q1", "value": 3}]})
    store.patch(root, specs, "answers.json", {"rows": [{"id": "Q1", "value": 5}]})
    data = json.loads((root / "submission/answers.json").read_text(encoding="utf-8"))
    rows.append(_report("jsonUpsertByItemKey",
                        data.get("answers") == [{"id": "Q1", "value": 5}], data=data))

    # 未声明交付物必须拒绝（防止写任意文件绕过契约）
    try:
        store.patch(root, specs, "etc/passwd", {"content": "x"})
        rows.append(_report("undeclaredRejected", False))
    except ValueError:
        rows.append(_report("undeclaredRejected", True))

    # 中断注入：30% / 60% / 90% 轮数处停止后，交付物仍存在且可解析
    for pct in (30, 60, 90):
        store.patch(root, specs, "deal_points.csv",
                    {"rows": [{"agreement": f"P{pct}", "deal_point": "x", "answer": "Y"}]})
        store.patch(root, specs, "answers.json", {"rows": [{"id": f"Q{pct}", "value": pct}]})
        store.patch(root, specs, "report.md", {"content": f"第 {pct}% 轮中断时的分析结论。\n"})
        missing, empty = store.missing_or_empty(root, specs)
        parsed = True
        try:
            json.loads((root / "submission/answers.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            parsed = False
        rows.append(_report(f"interruptAt{pct}Pct",
                            not missing and not empty and parsed,
                            missing=missing, empty=empty))

    # 完整性检查能发现"只有骨架无内容"
    empty_root = work / "ws-empty"
    (empty_root / "submission").mkdir(parents=True, exist_ok=True)
    store.ensure_skeleton(empty_root, specs)
    _, empty_names = store.missing_or_empty(empty_root, specs)
    rows.append(_report("detectsEmptySkeleton", "deal_points.csv" in empty_names,
                        empty=empty_names))

    # 请求文本推导：点名输出文件时自动建契约
    derived = store.derive_from_request("请把结果输出为 deal_points.csv 并附 summary.md")
    rows.append(_report("deriveFromRequest",
                        [d["name"] for d in derived] == ["deal_points.csv", "summary.md"],
                        derived=[d["name"] for d in derived]))
    return rows, all(r["ok"] for r in rows)


def _run_wrap_up() -> tuple[list[dict[str, Any]], bool]:
    """收尾模式判定 + 收尾期阻断检索（P0-04 第 3 条）。"""
    from lawbuddy.agent import autonomous

    rows: list[dict[str, Any]] = []
    # 预算充足 / 将尽
    rows.append(_report("wrapUpNotTriggeredEarly",
                        not autonomous._wrap_up_mode(10, 600, 40, 2400)))
    rows.append(_report("wrapUpTriggeredByRounds",
                        autonomous._wrap_up_mode(595, 600, 100, 2400)))
    rows.append(_report("wrapUpTriggeredByCalls",
                        autonomous._wrap_up_mode(100, 600, 2390, 2400)))
    # 收尾期禁止的必须包含检索类，且不包含交付回写
    blocked = autonomous._WRAP_UP_BLOCKED_TOOLS
    rows.append(_report("wrapUpBlocksSearch",
                        "document_search" in blocked and "document_get_pages" in blocked))
    rows.append(_report("wrapUpAllowsDeliverable",
                        "deliverable_patch" not in blocked))
    # 提示注入：收尾提示必须点名交付文件
    msgs: list[dict[str, Any]] = []
    state = {
        "deliverables": [{"name": "deal_points.csv", "format": "csv",
                          "columns": ["agreement", "deal_point", "answer"],
                          "key": ["agreement", "deal_point"]}],
    }
    autonomous._inject_deliverable_hint(msgs, state, 590, wrap_up=True)
    text = msgs[-1]["content"] if msgs else ""
    rows.append(_report("wrapUpHintMentionsFile",
                        "submission/deal_points.csv" in text and "收尾模式" in text))
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# P1-07 聚合工具：与官方 verify 对拍
# ---------------------------------------------------------------------------

def _reference_dir() -> Path | None:
    for cand in (Path("E:/project/_wfa_eval/reference"),
                 Path("E:/project/_wfa_eval")):
        if (cand / "verify.py").is_file():
            return cand
    return None


def _run_aggregate() -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy import aggregate

    rows: list[dict[str, Any]] = []
    ref = _reference_dir()
    if ref is None:
        # 无官方参考目录时只验证算子基本语义（CI 环境不依赖外部数据）
        table = {
            ("A01", "mae"): "Yes", ("A02", "mae"): "No", ("A03", "mae"): "Yes",
            ("A01", "multi"): frozenset({"x"}), ("A02", "multi"): frozenset({"x", "y"}),
            ("A03", "multi"): frozenset(),
        }
        agrs = ["A01", "A02", "A03"]
        rows.append(_report("countEq",
                            aggregate.compute({"op": "count", "cond": {"dp": "mae", "eq": "Yes"}},
                                              table, agrs) == 2))
        rows.append(_report("listEq",
                            aggregate.compute({"op": "list", "cond": {"dp": "mae", "eq": "Yes"}},
                                              table, agrs) == ["A01", "A03"]))
        rows.append(_report("distributionEq",
                            aggregate.compute({"op": "distribution", "dp": "mae",
                                               "options": ["Yes", "No"]}, table, agrs)
                            == {"Yes": 2, "No": 1}))
        rows.append(_report("nProfilesEq",
                            aggregate.compute({"op": "n_profiles", "dps": ["mae", "multi"]},
                                              table, agrs) == 3))
        rows.append(_report("hasCondEq",
                            aggregate.compute({"op": "count", "cond": {"dp": "multi", "has": "y"}},
                                              table, agrs) == 1))
        rows.append(_report("groupsEq",
                            aggregate.compute({"op": "groups", "dps": ["mae"]}, table, agrs)
                            == [["A01", "A03"]]))
        unsupported = False
        try:
            aggregate.compute({"op": "nope"}, table, agrs)
        except aggregate.AggregateError:
            unsupported = True
        rows.append(_report("unknownOpRejected", unsupported))
        return rows, all(r["ok"] for r in rows)

    sys.path.insert(0, str(ref))
    import verify  # type: ignore  # 官方评分器

    meta = json.loads((ref / "task_meta.json").read_text(encoding="utf-8"))
    spec = json.loads((ref / "merge_spec.json").read_text(encoding="utf-8"))
    gold = json.loads((ref / "answers.json").read_text(encoding="utf-8"))
    multi = {k for k, v in (meta.get("deal_points") or {}).items() if v.get("multi")}
    agreements = meta.get("agreements") or []
    table, warnings = aggregate.load_table(
        ref / "deal_points.csv", agreements=agreements, multi_points=multi,
    )
    rows.append(_report("tableLoaded", bool(table), cells=len(table), warnings=warnings))

    mismatch = 0
    for q in spec.get("questions") or []:
        mine = aggregate.compute(q, table, agreements)
        official = verify.compute(q, table, agreements, meta)
        if _norm(mine) != _norm(official):
            mismatch += 1
    rows.append(_report("matchesOfficialCompute", mismatch == 0, mismatch=mismatch,
                        questions=len(spec.get("questions") or [])))

    gold_diff = 0
    for q in spec.get("questions") or []:
        if _norm(aggregate.compute(q, table, agreements)) != _norm(gold.get(q["id"])):
            gold_diff += 1
    rows.append(_report("matchesGoldAnswers", gold_diff == 0, mismatch=gold_diff))

    # 与官方 evaluate 端到端对拍（§5.1 P1-07「与官方 verify.evaluate 对拍」）：
    # 用真值表 + 真值答案落一份 submission，双方各算一次，全部指标必须一致。
    import tempfile as _tf

    sub = Path(_tf.mkdtemp(prefix="wfa-sub-"))
    with (sub / "deal_points.csv").open("w", encoding="utf-8", newline="") as fh:
        import csv as _csv

        w = _csv.writer(fh)
        w.writerow(["agreement", "deal_point", "answer"])
        for (a, d), v in table.items():
            if isinstance(v, frozenset):
                w.writerow([a, d, " | ".join(sorted(v))])
            else:
                w.writerow([a, d, v])
    (sub / "answers.json").write_text(
        json.dumps(gold, ensure_ascii=False), encoding="utf-8",
    )
    official_eval = verify.evaluate(str(sub), str(ref))
    from evals.wfa_law.scorer import score_submission as _score

    fixture = {
        "name": "official-fidelity",
        "agreements": agreements,
        "dealPoints": list((meta.get("deal_points") or {})),
        "options": {k: v.get("options") or [] for k, v in (meta.get("deal_points") or {}).items()},
        "multi": {k: bool(v.get("multi")) for k, v in (meta.get("deal_points") or {}).items()},
        "table": {(a, d): (v if isinstance(v, str) else " | ".join(sorted(v)))
                  for (a, d), v in table.items()},
        "mergeSpec": spec.get("questions") or [],
        "goldAnswers": gold,
    }
    mine = _score(
        {(a, d): (v if isinstance(v, str) else " | ".join(sorted(v))) for (a, d), v in table.items()},
        gold, fixture, reference_dir=ref,
    )
    same = (
        abs(official_eval["branch"] - mine["branch"]) < 1e-9
        and abs(official_eval["merge"] - mine["merge"]) < 1e-9
        and abs(official_eval["merge_partial"] - mine["merge_partial"]) < 1e-9
        and abs(official_eval["consistency"] - mine["consistency"]) < 1e-9
        and official_eval["n_cells"] == mine["n_cells"]
    )
    rows.append(_report("matchesOfficialEvaluate", same,
                        official={k: official_eval[k] for k in
                                  ("branch", "merge", "merge_partial", "consistency")},
                        mine={k: mine[k] for k in
                              ("branch", "merge", "merge_partial", "consistency")}))
    return rows, all(r["ok"] for r in rows)


def _norm(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): int(v) if isinstance(v, (int, float)) else v for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return sorted(str(v) for v in value)
    return value


# ---------------------------------------------------------------------------
# P1-08 条款角色消歧
# ---------------------------------------------------------------------------

_SYMMETRIC_TEXT = (
    '"Company Material Adverse Effect" means any event, change or effect that has a '
    'material adverse effect on the Company.\n'
    '"Parent Material Adverse Effect" means any event, change or effect that has a '
    'material adverse effect on Parent.\n'
    '"Buyer Material Adverse Effect" means any effect that would prevent Buyer from '
    'consummating the transactions.\n'
)


def _run_clause_roles() -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy import clause_roles

    rows: list[dict[str, Any]] = []
    fams = clause_roles.families(_SYMMETRIC_TEXT)
    rows.append(_report("familyFound", len(fams) == 1, families=len(fams)))
    if fams:
        variants = {v["term"] for v in fams[0]["variants"]}
        rows.append(_report("bothSidesEnumerated",
                            any("Parent" in t for t in variants)
                            and any("Company" in t for t in variants),
                            variants=sorted(variants)))
        rows.append(_report("symmetricFlagged", bool(fams[0]["symmetric"])))

    aligned = clause_roles.audit(
        '"Company Material Adverse Effect" means any event, change or effect', "Target", _SYMMETRIC_TEXT,
    )
    rows.append(_report("targetWithTargetAligned", aligned["verdict"] == "aligned",
                        verdict=aligned["verdict"]))
    mismatch = clause_roles.audit(
        '"Parent Material Adverse Effect" means any event, change or effect', "Target", _SYMMETRIC_TEXT,
    )
    rows.append(_report("targetWithParentMismatch", mismatch["verdict"] == "mismatch",
                        verdict=mismatch["verdict"], hint=mismatch.get("hint", "")[:40]))
    acq = clause_roles.audit(
        '"Buyer Material Adverse Effect" means any effect that would prevent Buyer', "Buyer", _SYMMETRIC_TEXT,
    )
    rows.append(_report("buyerWithBuyerAligned", acq["verdict"] == "aligned"))
    unknown_subject = clause_roles.audit(
        '"Parent Material Adverse Effect" means', "不清楚", _SYMMETRIC_TEXT,
    )
    rows.append(_report("unknownSubjectNotDetermined", unknown_subject["verdict"] == "unknown"))
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# P2-09 多选逐项核对（无数据库依赖的路径）
# ---------------------------------------------------------------------------

def _run_options_check() -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy import options_check

    rows: list[dict[str, Any]] = []
    result = options_check.check({
        "subject": "mae_co_economic",
        "options": ["仅有字符串的候选", {"value": "缺引句"}, {"value": "有引句", "quote": "x", "documentId": "d", "pageNo": 1}],
    })
    rows.append(_report("stringsNotSelected", "仅有字符串的候选" not in result["selected"]))
    rows.append(_report("missingQuotePending",
                        any(p["value"] == "缺引句" for p in result["needsHuman"])))
    # 无该文档时引句回查必然失败 → 不选中
    rows.append(_report("unverifiableQuoteRejected",
                        "有引句" not in result["selected"] and result["allVerified"] is False))
    empty_rejected = False
    try:
        options_check.check({"options": []})
    except ValueError:
        empty_rejected = True
    rows.append(_report("emptyOptionsRejected", empty_rejected))

    # 交付契约声明的 multiPoints 必须活到规范化之后：它既是"多选需逐项核对"的
    # 判据，也是提示词里"逐项回查、以 | 连接"的依据。曾因规范化丢字段导致
    # 真实 LAW 任务拿不到 options_check 工具。
    from lawbuddy.agent import autonomous as _auto
    from lawbuddy.agent import deliverables as _deliverables

    specs = _deliverables.normalize_specs([{
        "name": "deal_points.csv", "format": "csv",
        "columns": ["agreement", "deal_point", "answer"],
        "key": ["agreement", "deal_point"],
        "multiPoints": ["mae_co_apc_scope", "mae_di_wnaf"],
    }])
    rows.append(_report("multiPointsSurviveNormalize",
                        specs and specs[0].get("multiPoints")
                        == ["mae_co_apc_scope", "mae_di_wnaf"], specs=specs))
    rows.append(_report("multiPointsInPrompt",
                        "mae_di_wnaf" in _deliverables.describe(specs)))
    state = {"mode": "legal_review", "request": "填写 deal point 表格", "deliverables": specs}
    names = _auto._exposed_tool_names(state) or set()
    rows.append(_report("multiPointsExposeOptionCheck", "options_check" in names))
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# P2-11 blob 生命周期
# ---------------------------------------------------------------------------

def _run_blob_lifecycle(work: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy import blobstore

    rows: list[dict[str, Any]] = []
    target = work / "ws-blob" / "blobs" / "ab"
    target.mkdir(parents=True, exist_ok=True)
    blob = target / "abc123"
    blob.write_bytes(b"x" * 64)
    import stat

    blob.chmod(stat.S_IREAD)  # 模拟原件只读
    result = blobstore.remove_tree(work / "ws-blob")
    rows.append(_report("readonlyBlobRemoved",
                        not (work / "ws-blob").exists() and not result["failed"],
                        removed=result["removed"], failed=result["failed"]))
    # 不存在的目录删除是幂等的
    rows.append(_report("removeMissingIdempotent",
                        blobstore.remove_tree(work / "nothing").get("removed") == 0))
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# P2-10 长输出分批与合并校验
# ---------------------------------------------------------------------------

def _run_long_output() -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy import long_output

    rows: list[dict[str, Any]] = []
    # 1) 实体数识别（中英文）
    rows.append(_report("detectChineseCount",
                        long_output.detect_entity_count("请生成 12 名被告的完整判决书") == 12))
    rows.append(_report("detectEnglishCount",
                        long_output.detect_entity_count("draft judgment for 9 defendants") == 9))
    rows.append(_report("detectNoCount",
                        long_output.detect_entity_count("请分析这份合同的违约条款") is None))

    # 2) 分批规划：12 个被告 → 3 批；每批 ≤4
    plan = long_output.plan_batches(12)
    rows.append(_report("planThreeBatches",
                        len(plan["batches"]) == 3 and plan["itemsPerBatch"] <= 4,
                        batches=plan["batches"]))
    # token 预算收紧时批容量随之下降
    tight = long_output.plan_batches(12, max_tokens=1200, tokens_per_item=600)
    rows.append(_report("tokenBudgetShrinksBatch",
                        tight["itemsPerBatch"] == 2 and len(tight["batches"]) == 6,
                        per=tight["itemsPerBatch"], n=len(tight["batches"])))
    rows.append(_report("zeroTotalNoBatch", long_output.plan_batches(0)["batches"] == []))

    # 3) 按主键合并：跨批同一人更新为后者、不同人追加
    fragments = [
        {"defendants": [{"name": "张三", "roles": {"a": 1}, "charges": {"a": 1}},
                        {"name": "李四", "roles": {"b": 1}, "charges": {"b": 1}}]},
        {"defendants": [{"name": "王五", "roles": {"c": 1}, "charges": {"c": 1}}]},
    ]
    merged = long_output.merge_fragments(fragments, "name", path=["defendants"])
    rows.append(_report("mergeAcrossBatches",
                        merged["count"] == 3 and merged["keys"] == ["张三", "李四", "王五"],
                        keys=merged["keys"]))
    # 冲突不静默覆盖：两条都报出
    conflict = long_output.merge_fragments(
        [{"name": "张三", "roles": {"a": 1}}, {"name": "张三", "roles": {"a": 2}}], "name",
    )
    rows.append(_report("conflictSurfaced",
                        len(conflict["merged"]) == 1 and len(conflict["conflicts"]) == 1))

    # 4) 合并后完整性校验：缺人 / roles-charges 不一致 / 重复
    valid = long_output.validate_merged(
        [{"name": "张三", "roles": {"a": 1}, "charges": {"a": 1}},
         {"name": "李四", "roles": {"b": 1}, "charges": {"b": 1}}],
        "name", expected=["张三", "李四"],
    )
    rows.append(_report("validPasses", valid["ok"], issues=valid["issues"]))
    missing = long_output.validate_merged([{"name": "张三"}], "name", expected=["张三", "李四"])
    rows.append(_report("missingEntityReported",
                        any(i["code"] == "MISSING_ENTITY" for i in missing["issues"])))
    mismatch = long_output.validate_merged(
        [{"name": "张三", "roles": {"a": 1}, "charges": {"b": 1},
          "roles2": {}, "charges2": {}}], "name",
    )
    rows.append(_report("rolesChargesMismatchReported",
                        any(i["code"] == "ROLES_CHARGES_MISMATCH" for i in mismatch["issues"])))
    dup = long_output.validate_merged([{"name": "张三"}, {"name": "张三"}], "name")
    rows.append(_report("duplicateReported",
                        any(i["code"] == "DUPLICATE_KEY" for i in dup["issues"])))
    rows.append(_report("batchHintMentionsRanges", "第 1–4 个" in long_output.batch_hint(12)))
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# P1-07 一致性校验（防「表 A 答 B」）
# ---------------------------------------------------------------------------

def _run_aggregate_consistency() -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy import aggregate

    rows: list[dict[str, Any]] = []
    table = {("A01", "mae"): "Yes", ("A02", "mae"): "No", ("A03", "mae"): "Yes"}
    agrs = ["A01", "A02", "A03"]
    q = {"id": "M01", "op": "count", "cond": {"dp": "mae", "eq": "Yes"}}
    ok = aggregate.check_consistency(q, table, agrs, 2)
    rows.append(_report("consistentAnswerPasses", ok["consistent"] is True, detail=ok))
    bad = aggregate.check_consistency(q, table, agrs, 3)
    rows.append(_report("wrongAnswerFlagged",
                        bad["consistent"] is False and bad["expected"] == 2 and bad["got"] == 3))
    # 字符串数字等价
    rows.append(_report("numericStringEquivalent",
                        aggregate.check_consistency(q, table, agrs, "2")["consistent"] is True))
    # 列表题忽略顺序
    lq = {"id": "M02", "op": "list", "cond": {"dp": "mae", "eq": "Yes"}}
    rows.append(_report("listOrderInsensitive",
                        aggregate.check_consistency(lq, table, agrs, ["A03", "A01"])["consistent"]))
    rows.append(_report("listMissingFlagged",
                        aggregate.check_consistency(lq, table, agrs, ["A01"])["consistent"] is False))
    # 分布题忽略零值差异
    dq = {"id": "M03", "op": "distribution", "dp": "mae", "options": ["Yes", "No"]}
    rows.append(_report("distributionZeroIgnored",
                        aggregate.check_consistency(dq, table, agrs,
                                                    {"Yes": 2, "No": 1, "Maybe": 0})["consistent"]))
    rows.append(_report("distributionWrongCountFlagged",
                        aggregate.check_consistency(dq, table, agrs,
                                                    {"Yes": 1, "No": 2})["consistent"] is False))
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# P1-06 补充：submission 可写 + 迁移期警告模式
# ---------------------------------------------------------------------------

def _run_readonly_extras(work: Path) -> tuple[list[dict[str, Any]], bool]:
    from lawbuddy import sandbox

    rows: list[dict[str, Any]] = []
    root = work / "ws-ro"
    for sub in ("inputs", "work", "submission"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "inputs/a.txt").write_text("原件", encoding="utf-8")

    state = {"task_id": "t", "case_id": "c", "tool_policy": {}}

    # 1) 交付目录可正常写入与读取（P0-04 回写依赖）
    sandbox.write_file(root, "submission/deal_points.csv", "agreement,answer\nA01,Yes\n")
    out = sandbox.read_file(root, "submission/deal_points.csv", 1, 10, state)
    rows.append(_report("submissionWritableAndReadable", "A01,Yes" in out["content"]))

    # 2) 默认模式：原件区硬拦截
    blocked = False
    try:
        sandbox.read_file(root, "inputs/a.txt", 1, 10, state)
    except ValueError:
        blocked = True
    rows.append(_report("inputsBlockedByDefault", blocked))

    # 3) 迁移期警告模式：放行并带回告警，且仍写审计
    sandbox._READONLY_WARN_ONLY = True
    try:
        warned = sandbox.read_file(root, "inputs/a.txt", 1, 10, state)
        rows.append(_report("warnModeAllowsReadWithWarning",
                            "原件" in warned.get("content", "")
                            and "迁移期警告模式" in str(warned.get("warning") or "")))
        bash_warn = sandbox._guard_bash_command(root, "cat inputs/a.txt", state)
        rows.append(_report("warnModeBashReturnsWarning",
                            bool(bash_warn) and "迁移期" in str(bash_warn)))
    finally:
        sandbox._READONLY_WARN_ONLY = False

    # 4) 写原件区在任何模式下都拒绝（产品硬约束）
    write_blocked = False
    sandbox._READONLY_WARN_ONLY = True
    try:
        sandbox.write_file(root, "inputs/a.txt", "篡改")
    except ValueError:
        write_blocked = True
    finally:
        sandbox._READONLY_WARN_ONLY = False
    rows.append(_report("writeInputsAlwaysBlocked",
                        write_blocked
                        and (root / "inputs/a.txt").read_text(encoding="utf-8") == "原件"))

    # 5) 审批通过后生命周期必须复位为 RUNNING。等待审批期间任务被置为
    # WAITING_APPROVAL；若批准后不复位，UI 会一直显示"等待审批"，且 cancel()
    # 走审批分支而非执行中分支。真实 LAW-001 跑批曾因此卡在 WAITING_APPROVAL。
    from lawbuddy import policy as _policy
    from lawbuddy.agent import tasks as _tasks
    from lawbuddy.agent.tasks import create_task as _create_task
    from lawbuddy.cases import create_case as _create_case
    from lawbuddy.workspace import ensure_workspace as _ensure_ws

    _ensure_ws(str(root))
    case = _create_case(title="只读区审批复位", case_type="commercial", client_name="测试")
    task = _create_task(case_id=case["id"], mode="legal_review", request_text="执行一条本地命令")
    req = _policy.create_approval(task["id"], case["id"], _policy.CAP_SHELL_EXEC,
                                  "echo 1", "ro-hash", "在本地工作空间执行 echo")
    _policy.respond(req["approvalId"], "allow_task")
    state2 = {
        "task_id": task["id"], "case_id": case["id"],
        "conversation_id": case["id"], "tool_policy": {},
    }
    sandbox.execute_tool(state2, "bash", {"command": "echo ok"})
    rows.append(_report("approvedShellResumesRunning",
                        _tasks.get_task(task["id"])["lifecycle"] == "RUNNING",
                        lifecycle=_tasks.get_task(task["id"])["lifecycle"]))
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# P2-11 工作空间删除 → 重建 → 重新导入
# ---------------------------------------------------------------------------

def _run_workspace_rebuild(work: Path) -> tuple[list[dict[str, Any]], bool]:
    """删除工作空间（含只读 blob）后重建并重新导入，全部文档应为 ready。

    这是方案 §5.1 P2-11 的验收条件；此前只读 blob 不清属性会导致删除失败，
    重建后旧 blob 用新 DEK 解密又统一报 parse failed。
    """
    from lawbuddy import documents, workspace
    from lawbuddy.cases import create_case
    from lawbuddy.workspace import ensure_workspace

    rows: list[dict[str, Any]] = []
    ws_root = work / "ws-rebuild"
    ensure_workspace(str(ws_root))

    src_dir = work / "src-docs"
    src_dir.mkdir(parents=True, exist_ok=True)
    docs: list[Path] = []
    for i in range(3):
        p = src_dir / f"A{i + 1:02d}.txt"
        p.write_text(
            (f"第{i + 1}份协议 第一条 定义 “Material Adverse Effect” means any event.\n" * 40),
            encoding="utf-8",
        )
        docs.append(p)

    case = create_case(title="重建验证", case_type="commercial", client_name="测试")
    first = documents.import_documents(case["id"], [str(p) for p in docs])
    ready_first = sum(1 for r in first if r.get("status") in ("imported", "ready", "partial"))
    rows.append(_report("initialImportReady", ready_first == len(docs),
                        imported=ready_first, of=len(docs)))

    # blob 必须是只读的（原件不可篡改）
    blob_root = ws_root / workspace.BLOB_DIRNAME
    blobs = [p for p in blob_root.rglob("*") if p.is_file()]
    readonly_ok = bool(blobs) and all(not (p.stat().st_mode & stat.S_IWRITE) for p in blobs)
    rows.append(_report("blobsAreReadonly", readonly_ok, blobs=len(blobs)))

    # 关闭当前库连接后删除 → 重建（模拟用户删除工作空间）
    from lawbuddy.storage import reset_database

    reset_database()
    removed = workspace.remove_workspace_dir(ws_root)
    rows.append(_report("workspaceRemoved", not ws_root.exists() and not removed["failed"],
                        removed=removed))

    ensure_workspace(str(ws_root))
    case2 = create_case(title="重建验证2", case_type="commercial", client_name="测试")
    second = documents.import_documents(case2["id"], [str(p) for p in docs])
    ready_second = sum(1 for r in second if r.get("status") in ("imported", "ready", "partial"))
    failures = [r for r in second if r.get("status") == "failed"]
    rows.append(_report("reimportAllReady", ready_second == len(docs) and not failures,
                        imported=ready_second, of=len(docs), failures=failures[:2]))
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# P0-04 端到端：真实 Loop + mock 网关 → 官方评分器读出非零分
# ---------------------------------------------------------------------------

def _e2e_steps(cells: list[tuple[str, str, str]], answers: dict[str, Any], final: str):
    """构造 mock 脚本：逐格回写 → 写聚合答案 → 收尾。"""
    from evals._loop_mock import ScriptStep

    steps = []
    for agreement, dp, answer in cells:
        steps.append(ScriptStep.tool("deliverable_patch", {
            "name": "deal_points.csv",
            "rows": [{"agreement": agreement, "deal_point": dp, "answer": answer}],
        }))
    if answers:
        steps.append(ScriptStep.tool("deliverable_patch", {
            "name": "answers.json", "merge": answers,
        }))
    steps.append(ScriptStep.final(final))
    return steps


def _run_p0_e2e(work: Path) -> tuple[list[dict[str, Any]], bool]:
    """跑一次完整 Agent 任务，验证"任何中断都有可评分交付物"（§5.1 P0-04）。

    - 完整跑：交付物齐全时官方评分器应读出满分（证明链路端到端可用）；
    - 中断注入：在 30% / 60% / 90% 处截断脚本（模拟预算耗尽/进程中断），
      交付物仍存在、可解析，且评分器读出非零分（允许低分，不允许 0 交付）。
    """
    import time

    from evals._loop_mock import install_mock_gateway
    from evals.wfa_law.fixture import build_mini_fixture
    from evals.wfa_law.scorer import score_submission
    from lawbuddy.agent.runner import AgentRunner
    from lawbuddy.agent.tasks import create_task, get_task
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.sandbox import workspace_root
    from lawbuddy.workspace import ensure_workspace

    rows: list[dict[str, Any]] = []
    fixture = build_mini_fixture()
    ref = None
    for cand in (Path("E:/project/_wfa_eval/reference"),):
        if (cand / "verify.py").is_file():
            ref = cand

    ws_root = work / "ws-e2e"
    ensure_workspace(str(ws_root))
    src = work / "e2e-docs"
    src.mkdir(parents=True, exist_ok=True)
    doc = src / "A01.txt"
    doc.write_text(
        "第 1 条 “Material Adverse Effect” means any event, change or effect.\n" * 30,
        encoding="utf-8",
    )
    case = create_case(title="P0-04 端到端", case_type="commercial", client_name="测试")
    import_documents(case["id"], [str(doc)])

    contract = [
        {"name": "deal_points.csv", "format": "csv",
         "columns": ["agreement", "deal_point", "answer"],
         "key": ["agreement", "deal_point"]},
        {"name": "answers.json", "format": "json", "skeleton": {}},
    ]

    gold_cells = [
        (a, d, v) for (a, d), v in fixture["table"].items()
    ]
    full_cells = [c for c in gold_cells]
    answers = dict(fixture["goldAnswers"])

    def run_once(name: str, cells, ans: dict[str, Any], final: str) -> dict[str, Any]:
        undo = install_mock_gateway(_e2e_steps(cells, ans, final))
        try:
            task = create_task(
                case_id=case["id"], mode="legal_review",
                request_text="请阅读协议并按交付契约填写 deal_points.csv 与 answers.json",
                deliverables=contract,
            )
            AgentRunner.instance().start(task["id"])
            deadline = time.monotonic() + 120
            info = get_task(task["id"])
            while time.monotonic() < deadline:
                info = get_task(task["id"])
                if info["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS",
                                         "CANCELLED", "FAILED"):
                    break
                time.sleep(0.1)
        finally:
            undo()
        sub = workspace_root() / "submission"
        table: dict[tuple[str, str], str] = {}
        csv_path = sub / "deal_points.csv"
        if csv_path.is_file():
            import csv as _csv

            with csv_path.open(encoding="utf-8-sig", newline="") as fh:
                for row in _csv.DictReader(fh):
                    a = str(row.get("agreement") or "").strip().upper()
                    d = str(row.get("deal_point") or "").strip()
                    if a and d:
                        table[(a, d)] = str(row.get("answer") or "")
        got_answers: dict[str, Any] = {}
        json_path = sub / "answers.json"
        if json_path.is_file():
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    got_answers = data
            except json.JSONDecodeError:
                got_answers = {}
        score = score_submission(table, got_answers, fixture, reference_dir=ref)
        return {"lifecycle": info["lifecycle"], "cells": len(table),
                "score": score, "submission": str(sub.name)}

    def cleanup() -> None:
        sub = workspace_root() / "submission"
        if sub.is_dir():
            import shutil

            shutil.rmtree(sub, ignore_errors=True)

    # 1) 完整跑：全格写入 + 全题答案 → 满分
    cleanup()
    full = run_once("full", full_cells, answers, "已完成全部协议 deal point 填写与聚合。")
    rows.append(_report("fullRunScoredPerfect",
                        full["score"]["branch"] == 1.0 and full["score"]["merge"] == 1.0,
                        lifecycle=full["lifecycle"], branch=full["score"]["branch"],
                        merge=full["score"]["merge"], cells=full["cells"]))
    rows.append(_report("fullRunUsesOfficialScorer", full["score"]["scorer"] == "official",
                        scorer=full["score"]["scorer"]))

    # 2) 中断注入：30% / 60% / 90% 处截断（只写入部分格、不写聚合答案）
    total = len(full_cells)
    for pct in (30, 60, 90):
        cleanup()
        keep = max(1, int(total * pct / 100))
        part = run_once(f"interrupt{pct}", full_cells[:keep], {}, f"预算在 {pct}% 处耗尽，已回写部分结论。")
        sc = part["score"]
        rows.append(_report(f"interrupt{pct}PctDeliverableScored",
                            part["cells"] >= keep and sc["branch"] > 0.0
                            and part["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS"),
                            cells=part["cells"], branch=sc["branch"], merge=sc["merge"],
                            lifecycle=part["lifecycle"]))
    cleanup()
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# P1-08 / P2-09 端到端：经真实 Loop 与工具分发调用校验工具
# ---------------------------------------------------------------------------

_ROLE_DOC_TEXT = (
    "第 3 条 定义。“Company Material Adverse Effect” means any event, change or "
    "effect that has a material adverse effect on the Company.\n"
    "“Parent Material Adverse Effect” means any event, change or effect that has a "
    "material adverse effect on Parent.\n"
)


def _run_role_option_e2e(work: Path) -> tuple[list[dict[str, Any]], bool]:
    """经完整 Agent Loop 调用 clause_role_check / options_check。

    单元评测直接调库函数，只能证明函数正确；这一段证明「工具确实被暴露、
    参数被正确传递、结果回传给模型」——即模型在真实任务里能用上它们。
    """
    import time

    from evals._loop_mock import ScriptStep, install_mock_gateway, last_tool_result
    from lawbuddy.agent.runner import AgentRunner
    from lawbuddy.agent.tasks import create_task, get_task
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.workspace import ensure_workspace

    rows: list[dict[str, Any]] = []
    ws_root = work / "ws-role"
    ensure_workspace(str(ws_root))
    src = work / "role-docs"
    src.mkdir(parents=True, exist_ok=True)
    doc = src / "mae.txt"
    doc.write_text(_ROLE_DOC_TEXT, encoding="utf-8")
    case = create_case(
        title="角色消歧端到端", case_type="commercial", client_name="测试",
        default_mode="contract_review",
    )
    imported = import_documents(case["id"], [str(doc)])
    doc_id = next((r.get("documentId") for r in imported if r.get("documentId")), None)
    rows.append(_report("docImported", bool(doc_id), docId=doc_id))
    if not doc_id:
        return rows, False

    state: dict[str, Any] = {}

    def role_args(_messages):
        return {
            "documentId": doc_id,
            "quote": "“Parent Material Adverse Effect” means any event, change or effect",
            "subject": "Target",
        }

    def option_args(_messages):
        return {
            "subject": "mae_co_economic",
            "options": ["仅字符串候选", {"value": "无引句候选"}],
        }

    def final_text(messages):
        # 读取最后一个工具结果，把两项校验结论带回给调用方
        last = last_tool_result(messages)
        state["last"] = last
        return "已完成同族条款校验与多选核对。"

    steps = [
        ScriptStep.tool("clause_role_check", role_args),
        # 记录第一次结果（脚本按序推进，这里用 tool_text 让下一步能读到上一步结果）
        ScriptStep.final(final_text),
    ]
    undo = install_mock_gateway(steps)
    try:
        task = create_task(
            case_id=case["id"], mode="contract_review",
            request_text="请核对 Target 的 Material Adverse Effect 定义（同族条款消歧）",
        )
        AgentRunner.instance().start(task["id"])
        deadline = time.monotonic() + 90
        info = get_task(task["id"])
        while time.monotonic() < deadline:
            info = get_task(task["id"])
            if info["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS",
                                     "CANCELLED", "FAILED"):
                break
            time.sleep(0.1)
    finally:
        undo()

    verdict = (state.get("last") or {}).get("verdict")
    rows.append(_report("roleCheckToolDispatched",
                        info["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS"),
                        lifecycle=info["lifecycle"]))
    rows.append(_report("roleCheckDetectsMismatch", verdict == "mismatch", verdict=verdict))

    # 多选核对：经同一 Loop 的另一任务，确认工具可调用且不放行无证据项
    state2: dict[str, Any] = {}
    case2 = create_case(title="多选逐项核对端到端", case_type="commercial", client_name="测试")
    import_documents(case2["id"], [str(doc)])

    def final_text2(messages):
        state2["last"] = last_tool_result(messages)
        return "已完成多选逐项核对。"

    undo = install_mock_gateway([
        ScriptStep.tool("options_check", option_args),
        ScriptStep.final(final_text2),
    ])
    try:
        task2 = create_task(
            case_id=case2["id"], mode="legal_review",
            request_text="请核对多选题：哪些情形构成 economic material adverse effect（全部列出）",
        )
        AgentRunner.instance().start(task2["id"])
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            get_task(task2["id"])
            time.sleep(0.1)
            if get_task(task2["id"])["lifecycle"] in (
                "COMPLETED", "COMPLETED_WITH_WARNINGS", "CANCELLED", "FAILED",
            ):
                break
    finally:
        undo()
    opt = state2.get("last") or {}
    rows.append(_report("optionsCheckToolDispatched", "selected" in opt, keys=sorted(opt)[:6]))
    rows.append(_report("optionsCheckRejectsUnsourced",
                        opt.get("selected") == [] and bool(opt.get("needsHuman")),
                        selected=opt.get("selected"), needsHuman=len(opt.get("needsHuman") or [])))
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# P1-06 端到端：真实任务中直读原件的次数应为 0
# ---------------------------------------------------------------------------

def _run_direct_read_e2e(work: Path) -> tuple[list[dict[str, Any]], bool]:
    """LAW-001 验收：完整任务里 shell/文件工具直读原件的次数归零。

    单元评测证明"读会被拒"，但方案 §5.1 P1-06 要求的是**任务层面**不再出现
    直读原件：模型必须改用 document_search / document_get_pages。这里用 mock
    脚本让模型真的尝试 `bash cat inputs/...` 与 `read_file inputs/...`，
    再检查：① 两次都被拒绝且带可执行指引；② 审计里有两条拦截记录；
    ③ 模型改走文档工具后能拿到带页码的原文。
    """
    import time

    from evals._loop_mock import ScriptStep, install_mock_gateway, last_tool_result
    from lawbuddy.agent.runner import AgentRunner
    from lawbuddy.agent.tasks import create_task, get_events, get_task
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.sandbox import readonly_zone, workspace_root
    from lawbuddy.storage import get_db
    from lawbuddy.workspace import ensure_workspace

    rows: list[dict[str, Any]] = []
    ws_root = work / "ws-read"
    ensure_workspace(str(ws_root))
    # 原件落进 inputs/（用户导入的原件区）
    inputs = workspace_root() / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    origin = inputs / "A01.txt"
    origin.write_text(
        "第 1 条 “Material Adverse Effect” means any event, change or effect.\n" * 20,
        encoding="utf-8",
    )
    rows.append(_report("inputsZoneRecognized",
                        readonly_zone(workspace_root(), origin) == "inputs"))

    case = create_case(title="直读拦截端到端", case_type="commercial", client_name="测试")
    imported = import_documents(case["id"], [str(origin)])
    doc_id = next((r.get("documentId") for r in imported if r.get("documentId")), None)
    rows.append(_report("originImported", bool(doc_id)))
    if not doc_id:
        return rows, False

    captured: dict[str, Any] = {}

    def final_text(messages):
        captured["last"] = last_tool_result(messages)
        return "已通过文档工具读取原件。"

    steps = [
        ScriptStep.tool("bash", {"command": "cat inputs/A01.txt", "purpose": "直接读原件"}),
        ScriptStep.tool("read_file", {"path": "inputs/A01.txt"}),
        ScriptStep.tool("document_get_pages", {"documentId": doc_id, "pageStart": 1, "pageEnd": 1}),
        ScriptStep.final(final_text),
    ]
    undo = install_mock_gateway(steps)
    try:
        task = create_task(
            case_id=case["id"], mode="legal_review",
            # 请求里点名"脚本/表格"是为了让沙箱工具（bash/read_file）真实下发——
            # 真实 LAW 任务里模型正是这样拿到了直读通道；不下发就测不到拦截。
            request_text=(
                "请核对本案协议第 1 条的 Material Adverse Effect 定义（阅卷），"
                "并写一个脚本把要点整理成 csv 表格"
            ),
            selected_document_ids=[doc_id],
        )
        AgentRunner.instance().start(task["id"])
        deadline = time.monotonic() + 90
        info = get_task(task["id"])
        while time.monotonic() < deadline:
            info = get_task(task["id"])
            if info["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS",
                                     "CANCELLED", "FAILED"):
                break
            time.sleep(0.1)
    finally:
        undo()

    events = get_events(task["id"])
    tool_results = [
        e["payload"] for e in events
        if e["type"] == "tool_result_message" and e["payload"].get("status") == "error"
    ]
    errors = [str(r.get("result")) for r in tool_results]
    rows.append(_report("bashBlocked",
                        any("只读原件区" in t for t in errors), n=len(errors)))
    rows.append(_report("readFileBlocked",
                        any("只读原件区" in t for t in errors)
                        and any("document_search" in t for t in errors),
                        hints=len([t for t in errors if "document_search" in t])))
    # 文档工具仍可用且返回带页码的原文
    pages = captured.get("last") or {}
    rows.append(_report("documentToolStillWorks",
                        bool(pages.get("pages")) or "page" in json.dumps(pages)[:200],
                        keys=sorted(pages)[:6]))
    # 审计留痕（只记路径/动作，不含原文）
    hits = get_db().query(
        "SELECT metadata_json FROM audit_logs WHERE action = 'sandbox.readonly_blocked'"
    )
    metas = [json.loads(r["metadata_json"] or "{}") for r in hits]
    rows.append(_report("blockedAuditedOnTask",
                        any(m.get("action") in ("bash", "read") for m in metas)
                        and all("content" not in m for m in metas),
                        events=len(metas)))
    rows.append(_report("noDirectReadSucceeded",
                        all("Material Adverse Effect" not in (
                            json.dumps(r.get("result"), ensure_ascii=False)
                            if r.get("status") == "ok" else ""
                        ) for r in (e["payload"] for e in events
                                    if e["type"] == "tool_result_message"
                                    and e["payload"].get("name") in ("bash", "read_file")))))
    return rows, all(r["ok"] for r in rows)


def _run_criminal_repeat(work: Path) -> tuple[list[dict[str, Any]], bool]:
    """§5.1 P2-10 验收：12 被告任务连续 3 次完整交付。

    每次都走真实 Loop：分批回写 12 个实体到交付物、合并校验、写聚合答案，
    三次都必须 12/12 齐全且聚合口径一致（模拟 LAW-016 的重复运行）。
    """
    import time

    from evals._loop_mock import ScriptStep, install_mock_gateway, last_tool_result
    from evals.wfa_law.fixture import build_criminal_fixture
    from lawbuddy import long_output
    from lawbuddy.agent.runner import AgentRunner
    from lawbuddy.agent.tasks import create_task, get_events, get_task
    from lawbuddy.cases import create_case
    from lawbuddy.documents import import_documents
    from lawbuddy.sandbox import workspace_root
    from lawbuddy.workspace import ensure_workspace

    rows: list[dict[str, Any]] = []
    fixture = build_criminal_fixture()
    ws_root = work / "ws-crim"
    ensure_workspace(str(ws_root))
    src = work / "crim-docs"
    src.mkdir(parents=True, exist_ok=True)
    doc = src / "judgment.txt"
    doc.write_text(
        "刑事判决书 被告人 D01 至 D12，分别实施下列行为。\n" * 30,
        encoding="utf-8",
    )
    case = create_case(title="12 被告重复交付", case_type="criminal", client_name="测试")
    import_documents(case["id"], [str(doc)])

    plan = long_output.plan_batches(len(fixture["expectedEntities"]))
    contract = [
        {"name": "answers.json", "format": "json", "skeleton": {},
         "path": ["answers"], "itemKey": "id"},
        {"name": "defendants.json", "format": "json", "skeleton": {},
         "path": ["defendants"], "itemKey": "defendant"},
    ]

    def _collect(root: Path) -> list[dict[str, Any]]:
        path = root / "submission/defendants.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return list((data or {}).get("defendants") or [])
        except (OSError, json.JSONDecodeError):
            return []

    def build_steps() -> list:
        steps = []
        # 逐批回写被告节点（每批 ≤4 人，模拟分批生成）
        for b in plan["batches"]:
            batch = [
                {"defendant": d,
                 "roles": {c: True for c in fixture["charges"][d]},
                 "charges": {c: True for c in fixture["charges"][d]}}
                for d in fixture["expectedEntities"][b["start"] - 1: b["end"]]
            ]
            steps.append(ScriptStep.tool("deliverable_patch", {
                "name": "defendants.json", "rows": batch,
            }))
        steps.append(ScriptStep.tool("merge_fragments_check", lambda messages: {
            "fragments": [{"defendants": _collect(workspace_root())}],
            "keyField": "defendant",
            "path": ["defendants"],
            "expectedEntities": fixture["expectedEntities"],
        }))
        steps.append(ScriptStep.tool("deliverable_patch", {
            "name": "answers.json",
            # answers.json 的声明是 {path: ["answers"], itemKey: "id"}，
            # 因此按数组 upsert 写入（merge 会改成对象形态，和 path 冲突）
            "rows": [{"id": k, "value": v} for k, v in fixture["goldAnswers"].items()],
        }))
        steps.append(ScriptStep.final(
            lambda messages: f"已完成 12 名被告分批交付（校验：{last_tool_result(messages).get('ok')}）。"
        ))
        return steps

    runs: list[dict[str, Any]] = []
    for attempt in range(3):
        sub = workspace_root() / "submission"
        if sub.is_dir():
            import shutil

            shutil.rmtree(sub, ignore_errors=True)
        undo = install_mock_gateway(build_steps())
        try:
            task = create_task(
                case_id=case["id"], mode="legal_review",
                request_text="请生成 12 名被告的完整判决要点（分批产出并合并校验）",
                deliverables=contract,
            )
            AgentRunner.instance().start(task["id"])
            deadline = time.monotonic() + 120
            info = get_task(task["id"])
            while time.monotonic() < deadline:
                info = get_task(task["id"])
                if info["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS",
                                         "CANCELLED", "FAILED"):
                    break
                time.sleep(0.1)
        finally:
            undo()
        collected = _collect(workspace_root())
        answers_path = workspace_root() / "submission/answers.json"
        answers: list[Any] = []
        try:
            answers = json.loads(answers_path.read_text(encoding="utf-8")).get("answers") or []
        except (OSError, json.JSONDecodeError, AttributeError):
            answers = []
        checks = get_events(task["id"])
        merge_event = next(
            (e for e in checks
             if e["type"] == "tool_result_message"
             and e["payload"].get("name") == "merge_fragments_check"), None,
        )
        merge_ok = bool(((merge_event or {}).get("payload") or {}).get("result", {}).get("ok"))
        runs.append({
            "attempt": attempt + 1,
            "entities": len(collected),
            "answers": len(answers),
            "mergeOk": merge_ok,
            "lifecycle": info["lifecycle"],
        })

    complete_all = all(
        r["entities"] == 12 and r["answers"] == len(fixture["goldAnswers"])
        and r["mergeOk"] and r["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS")
        for r in runs
    )
    rows.append(_report("threeConsecutiveFullDelivery", complete_all, runs=runs))

    # 断连注入（§5.1 P2-10）：长输出第 2 批生成中途连接被切断，Loop 应带着
    # 已生成部分请求续写，最终仍交出完整 12 名被告，而不是整轮丢弃重来。
    sub = workspace_root() / "submission"
    if sub.is_dir():
        import shutil

        shutil.rmtree(sub, ignore_errors=True)
    steps = build_steps()
    interrupt_at = 2
    partial_batch = [
        {"defendant": d,
         "roles": {c: True for c in fixture["charges"][d]},
         "charges": {c: True for c in fixture["charges"][d]}}
        for d in fixture["expectedEntities"][plan["batches"][interrupt_at]["start"] - 1:][:1]
    ]
    # 在第 3 批回写之前插入一次"中途断连"：已输出该批第一个被告的片段
    steps.insert(
        2 + interrupt_at,
        ScriptStep.interrupt(json.dumps({"defendants": partial_batch}, ensure_ascii=False)[:120]),
    )
    undo = install_mock_gateway(steps)
    try:
        task = create_task(
            case_id=case["id"], mode="legal_review",
            request_text="请生成 12 名被告的完整判决要点（分批产出并合并校验）",
            deliverables=contract,
        )
        AgentRunner.instance().start(task["id"])
        deadline = time.monotonic() + 120
        info = get_task(task["id"])
        while time.monotonic() < deadline:
            info = get_task(task["id"])
            if info["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS",
                                     "CANCELLED", "FAILED"):
                break
            time.sleep(0.1)
    finally:
        undo()
    resumed = _collect(workspace_root())
    events = get_events(task["id"])
    resume_notice = any(
        e["type"] == "text" and "输出中断" in str((e.get("payload") or {}).get("delta") or "")
        for e in events
    )
    rows.append(_report(
        "interruptResumesToFullDelivery",
        len(resumed) == 12 and resume_notice
        and info["lifecycle"] in ("COMPLETED", "COMPLETED_WITH_WARNINGS"),
        entities=len(resumed), resumeNotice=resume_notice, lifecycle=info["lifecycle"],
    ))
    return rows, all(r["ok"] for r in rows)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    report: dict[str, Any] = {"sections": {}, "gates": {}}
    work = Path(tempfile.mkdtemp(prefix="lawbuddy-m4-"))
    sections = (
        ("deliverables", lambda: _run_deliverables(work)),
        ("wrapUp", lambda: _run_wrap_up()),
        ("aggregate", lambda: _run_aggregate()),
        ("clauseRoles", lambda: _run_clause_roles()),
        ("optionsCheck", lambda: _run_options_check()),
        ("longOutput", lambda: _run_long_output()),
        ("aggregateConsistency", lambda: _run_aggregate_consistency()),
        ("readonlyExtras", lambda: _run_readonly_extras(work)),
        ("blobLifecycle", lambda: _run_blob_lifecycle(work)),
        ("workspaceRebuild", lambda: _run_workspace_rebuild(work)),
        ("p0DeliverableE2E", lambda: _run_p0_e2e(work)),
        ("roleOptionE2E", lambda: _run_role_option_e2e(work)),
        ("directReadE2E", lambda: _run_direct_read_e2e(work)),
        ("criminalRepeat", lambda: _run_criminal_repeat(work)),
    )
    passed = True
    for name, fn in sections:
        try:
            rows, ok = fn()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            rows, ok = [_report(name, False, error=f"{type(exc).__name__}: {exc}")], False
        report["sections"][name] = rows
        report["gates"][name] = ok
        passed = passed and ok

    print("\n=== M4 优化项评测（P0-04 / P1-07 / P1-08 / P2-09 / P2-11）===")
    for name, rows in report["sections"].items():
        mark = "PASS" if report["gates"][name] else "FAIL"
        print(f"\n[{mark}] {name}")
        for r in rows:
            flag = "ok  " if r["ok"] else "FAIL"
            extra = {k: v for k, v in r.items() if k not in ("name", "ok")}
            print(f"  {flag} {r['name']}" + (f"  {extra}" if extra else ""))
    print("\n结论：" + ("全部通过" if passed else "存在失败项"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
