"""WorkflowArena LAW 域回归评测（WFA-LAW 优化方案 §4.9 / P2-12）。

把这次优化用到的评分口径与聚合口径固化成可重复运行的评测包，避免每次
靠手工跑 22 个任务、人工对比分数。

结构：
- `fixture`   自带的最小任务镜像（3 协议 × 3 交付点 + 3 道聚合题），不依赖外部数据，
              用于验证评分与聚合口径的确定性（CI 快速档）；
- `scorer`    评分器：优先用官方 `verify.py`（存在时逐字复用，绝不自造口径），
              缺失时退化为与官方同语义的本包实现；
- `driver`    任务驱动：把协议原文导入案件、创建带交付契约的任务、收集
              `submission/`、调用评分器；
- `report`    结果 JSON 与回归 diff（同一 commit 两次运行应完全一致）。

用法（在 services/core 下）：
    python -m evals.wfa_law --profile fast
    python -m evals.wfa_law --profile full --data-dir E:/project/_wfa_eval --out out.json
    python -m evals.wfa_law --compare baseline.json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .fixture import build_mini_fixture
from .report import compare_reports
from .scorer import score_submission, self_check

EXIT_OK = 0
EXIT_FAIL = 1


def _reference_dir(data_dir: str | None) -> Path | None:
    """定位官方参考目录（含 verify.py + 任务真值）。

    两类任务的 reference 里都放 verify.py，但真值文件名不同：
    - 合同类：`task_meta.json` + `deal_points.csv` + `answers.json`；
    - 刑事类：`gold.json`。
    只认其中一种会把另一类任务误判成"缺参考目录"，所以两种都接受。
    """
    candidates: list[Path] = []
    if data_dir:
        base = Path(data_dir)
        candidates += [base / "reference", base]
    env = __import__("os").environ.get("WFA_LAW_DATA")
    if env:
        candidates += [Path(env) / "reference", Path(env)]
    candidates += [Path("E:/project/_wfa_eval/reference"), Path("../_wfa_eval/reference")]
    for cand in candidates:
        if not (cand / "verify.py").is_file():
            continue
        if (cand / "task_meta.json").is_file() or (cand / "gold.json").is_file():
            return cand
    return None


def _run_fast(reference: Path | None) -> dict[str, Any]:
    """快速档：2 个代表任务（合同 deal-point / 刑事多被告），验证评分与聚合口径。

    无模型、无外部数据也可跑：用真值表反打评分器，证明"口径未被改坏"；
    再对刑事镜像做分批规划 + 合并校验，证明 12 被告这类长输出不会丢人。
    """
    from .fixture import build_criminal_fixture, build_fast_fixtures

    fixtures = build_fast_fixtures()
    result: dict[str, Any] = {
        "profile": "fast",
        "fixtures": [f["name"] for f in fixtures],
        "checks": [],
    }

    def check(name: str, ok: bool, **extra: Any) -> None:
        result["checks"].append({"name": name, "ok": bool(ok), **extra})

    for fixture in fixtures:
        tag = fixture["name"]
        # 1) 用真值表作答必须得满分（评分器与自身口径自洽）
        perfect = score_submission(
            fixture["table"], fixture["goldAnswers"], fixture, reference_dir=reference,
        )
        check(f"{tag}.goldScoresPerfect",
              perfect["branch"] == 1.0 and perfect["merge"] == 1.0,
              branch=perfect["branch"], merge=perfect["merge"])
        # 2) 空提交必须 0 分且不报 full_success
        empty = score_submission({}, {}, fixture, reference_dir=reference)
        check(f"{tag}.emptyScoresZero", empty["branch"] == 0.0 and empty["merge"] == 0.0
              and not empty["full_success"])
        # 3) 部分正确给出 0~1 之间的确定性分数
        partial_table = dict(fixture["table"])
        first_key = sorted(partial_table)[0]
        partial_table[first_key] = "WRONG-OPTION"
        partial = score_submission(partial_table, fixture["goldAnswers"], fixture,
                                   reference_dir=reference)
        check(f"{tag}.partialIsDeterministic", 0.0 < partial["branch"] < 1.0,
              branch=partial["branch"])
        # 4) 同一输入重复评分结果完全一致（回归对比的前提）
        again = score_submission(partial_table, fixture["goldAnswers"], fixture,
                                 reference_dir=reference)
        check(f"{tag}.scoringIsStable", again["branch"] == partial["branch"]
              and again["merge"] == partial["merge"])
        # 5) 官方评分器可用时按其口径复算同一提交（避免双口径漂移）
        if reference is not None:
            official = _score_with_official(fixture, partial_table, reference)
            check(f"{tag}.matchesOfficialVerifier",
                  official is not None and abs(official["branch"] - partial["branch"]) < 1e-9,
                  officialBranch=(official or {}).get("branch"), ourBranch=partial["branch"])

    # 6) 聚合口径自检（算子与官方同语义）
    agg_ok, agg_detail = self_check()
    check("aggregateOperatorsMatch", agg_ok, **agg_detail)

    # 7) 刑事多被告：12 人必须分 3 批，合并后实体齐全、roles/charges 口径一致
    crim = build_criminal_fixture()
    from lawbuddy import long_output

    plan = long_output.plan_batches(len(crim["expectedEntities"]))
    check("criminal.batchedIntoThree", len(plan["batches"]) == 3,
          batches=plan["batches"])
    # 按批构造片段（模拟分批生成），合并后应恰好 12 人
    fragments = []
    for b in plan["batches"]:
        rows_ = []
        for d in crim["expectedEntities"][b["start"] - 1: b["end"]]:
            charges = {c: True for c in crim["charges"][d]}
            rows_.append({"defendant": d, "roles": dict(charges), "charges": charges})
        fragments.append({"defendants": rows_})
    merged = long_output.merge_fragments(fragments, "defendant", path=["defendants"])
    check("criminal.mergeKeepsAllEntities", merged["count"] == 12,
          keys=merged["keys"])
    report = long_output.validate_merged(
        merged["merged"], "defendant", expected=crim["expectedEntities"],
    )
    check("criminal.mergedPassesValidation", report["ok"], issues=report["issues"])
    # 少一批时必须报缺人（而不是静默给出 8/12）
    incomplete = long_output.validate_merged(
        merged["merged"][:8], "defendant", expected=crim["expectedEntities"],
    )
    check("criminal.missingBatchReported",
          any(i["code"] == "MISSING_ENTITY" for i in incomplete["issues"]))

    result["ok"] = all(c["ok"] for c in result["checks"])
    return result


def _score_with_official(fixture: dict[str, Any], table: dict[tuple[str, str], str],
                         reference: Path) -> dict[str, Any] | None:
    """把本包的表结构转成官方评分器输入并复算（仅用于口径对拍）。"""
    sys.path.insert(0, str(reference))
    try:
        import verify  # type: ignore
    except Exception:
        return None
    meta = {
        "agreements": fixture["agreements"],
        "deal_points": {
            dp: {"options": opts, "multi": bool(fixture["multi"].get(dp))}
            for dp, opts in fixture["options"].items()
        },
    }
    official_table: dict[tuple[str, str], Any] = {}
    for (a, d), value in table.items():
        if fixture["multi"].get(d):
            parts = [p.strip() for p in str(value).split("|") if p.strip()]
            official_table[(a, d)] = frozenset(p for p in parts if p in meta["deal_points"][d]["options"])
        else:
            official_table[(a, d)] = value
    gold_table = {}
    for (a, d), value in fixture["table"].items():
        if fixture["multi"].get(d):
            parts = [p.strip() for p in str(value).split("|") if p.strip()]
            gold_table[(a, d)] = frozenset(parts)
        else:
            gold_table[(a, d)] = value
    per_dp: dict[str, list[float]] = {}
    for (a, d), g in gold_table.items():
        s = official_table.get((a, d))
        per_dp.setdefault(d, []).append(verify.cell_score(g, s, bool(fixture["multi"].get(d))))
    cells = [x for v in per_dp.values() for x in v]
    branch = sum(cells) / len(cells)
    spec = {"questions": fixture["mergeSpec"]}
    exact = []
    for q in spec["questions"]:
        e, _p = verify.score_answer(q, fixture["goldAnswers"][q["id"]], None)
        exact.append(e)
    # 官方 score_answer 对 None 返回 0；这里只对拍 branch（合并题无提交内容）
    return {"branch": round(branch, 4)}


def _run_full(data_dir: str | None, out: Path | None, *,
              runtime: str = "v2", collect_harness_metrics: bool = False,
              fault: str | None = None, interrupt_at: int | None = None,
              task_filter: str | None = None,
              workspace: str | None = None) -> dict[str, Any]:
    """全量档：驱动任务（需要模型 API Key 与数据快照）。"""
    from .driver import resolve_reference, run_full_profile

    reference = _reference_dir(data_dir)
    base = Path(data_dir) if data_dir else (reference.parent if reference else None)
    if base is None:
        return {
            "profile": "full", "ok": False,
            "error": "缺少数据快照：请用 --data-dir 指定含 inputs/ 与 reference/ 的目录",
        }
    # 真实模型评测必须读本机同一份受保护设备配置（模型/服务商/API Key）：
    # 否则会在临时空 profile 下报「尚未添加模型」，造成与应用内不一致的分裂状态。
    from .. import use_device_profile

    use_device_profile()
    # --task 每个任务的真值不同：优先按任务解析 reference，再退回传入的公共目录。
    task_reference = resolve_reference(base, task_filter) or reference
    result = run_full_profile(
        base, task_reference, out=out, runtime=runtime,
        collect_harness_metrics=collect_harness_metrics,
        fault=fault, interrupt_at=interrupt_at, task_filter=task_filter,
        workspace_dir=(Path(workspace) / (task_filter or "default")) if workspace else None,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.wfa_law", description="WFA LAW 回归评测")
    # profile 同时接受既有 fast/full 与 B-06 的 directed（既有定向链路）/
    # autonomous（自主 Agent 链路）。directed 走 full 的真实任务驱动并钉 v1 运行时，
    # autonomous 同样走真实任务但钉 v2（工作账本 + 交付控制器），两者报告不得混算。
    parser.add_argument("--profile", choices=("fast", "full", "directed", "autonomous"),
                        default="fast",
                        help="fast=确定性评分自检；full=真实任务；"
                             "directed=既有定向链路（v1）；autonomous=自主链路（v2）")
    parser.add_argument("--data-dir", default=None, help="含 inputs/ 与 reference/ 的数据快照目录")
    parser.add_argument("--out", default=None, help="结果 JSON 输出路径")
    parser.add_argument("--compare", nargs=2, metavar=("BASE", "NEW"), default=None,
                        help="对比两次运行结果，输出回归 diff")
    # ---- B-06：驱动口径与观测选项（与既有 fast/full 并列） ----
    parser.add_argument("--runtime", choices=("v1", "v2"), default="v2",
                        help="运行时版本：v2 启用工作账本与交付控制器（默认），v1 对拍旧基线")
    parser.add_argument("--task", default=None, metavar="WFA-LAW-001",
                        help="只跑指定任务（读取 inputs/tasks/<id>/，不读 reference/）")
    parser.add_argument("--all-law", action="store_true",
                        help="跑 22 题全量：依次跑 inputs/tasks/ 下每个任务目录")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="同一配置连跑 N 次并输出中位数/最差值/方差")
    parser.add_argument("--interrupt-at", type=int, choices=(30, 60, 90), default=None,
                        help="在指定百分比处中断任务（验证部分交付可解析）")
    parser.add_argument("--inject-fault", choices=("disconnect", "timeout", "429"), default=None,
                        help="在模型网关注入一次故障，验证不产生静默空批次")
    parser.add_argument("--collect-harness-metrics", action="store_true",
                        help="输出 Harness 指标（结构交付率/终态率/Token 每提交格等）")
    parser.add_argument("--workspace", default=None,
                        help="隔离工作空间根目录；--repeat/--all-law 自动在其下按运行次数分目录")
    args = parser.parse_args(argv)

    if args.compare:
        base = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        new = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        diff = compare_reports(base, new)
        print(json.dumps(diff, ensure_ascii=False, indent=2))
        return EXIT_OK if diff["ok"] else EXIT_FAIL

    reference = _reference_dir(args.data_dir)
    # profile 与 runtime 的一致性（C-05）：定向链路固定 v1、自主链路固定 v2，
    # 防止用 v2 跑定向报告或反之，导致两个口径混算。
    if args.profile == "directed":
        args.runtime = "v1"
    elif args.profile == "autonomous":
        args.runtime = "v2"
    profile_label = args.profile if args.profile in ("directed", "autonomous") else "full"
    if args.profile == "fast":
        result = _run_fast(reference)
    elif args.all_law and args.data_dir:
        result = _run_all_law(args, reference)
    elif args.repeat > 1 and args.data_dir:
        result = _run_repeated(args, reference)
    else:
        result = _run_full(args.data_dir, Path(args.out) if args.out else None,
                           runtime=args.runtime,
                           collect_harness_metrics=args.collect_harness_metrics,
                           fault=args.inject_fault,
                           interrupt_at=args.interrupt_at,
                           task_filter=args.task,
                           workspace=args.workspace)
    if args.profile != "fast":
        result["profile"] = profile_label
        result["runtime"] = args.runtime

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return EXIT_OK if result.get("ok") else EXIT_FAIL


def _run_all_law(args: argparse.Namespace, reference: Path | None) -> dict[str, Any]:
    """22 题全量：逐任务目录跑一次并汇总平均分与结构交付率。"""
    from .driver import resolve_reference, run_full_profile

    base = Path(args.data_dir)
    tasks_dir = base / "inputs/tasks"
    if not tasks_dir.is_dir():
        return {"profile": "full", "allLaw": True, "ok": False,
                "error": f"缺少任务目录：{tasks_dir}"}
    task_ids = sorted(p.name for p in tasks_dir.iterdir() if p.is_dir())
    from .. import use_device_profile

    use_device_profile()
    runs: list[dict[str, Any]] = []
    for task_id in task_ids:
        run = run_full_profile(
            base, resolve_reference(base, task_id) or reference,
            out=None, runtime=args.runtime,
            collect_harness_metrics=args.collect_harness_metrics,
            fault=args.inject_fault, interrupt_at=args.interrupt_at,
            task_filter=task_id,
            workspace_dir=(Path(args.workspace) / task_id) if args.workspace else None,
        )
        run["taskId"] = task_id
        runs.append(run)
    branches = [r["branch"] for r in runs if isinstance(r.get("branch"), (int, float))]
    # 结构交付率按 structuralStatus 判定：未终态答案格会让综合 status 变 partial，
    # 但 C-05 只问「是否 22/22 都有结构交付物」。
    structures = [1.0 if ((r.get("harness") or {}).get("delivery") or {})
                  .get("structuralStatus") == "complete" else 0.0
                  for r in runs if r.get("harness")]
    summary = {
        "tasks": len(task_ids),
        "avgBranch": round(sum(branches) / len(branches), 4) if branches else 0.0,
        "structuralDeliveryRate": (round(sum(structures) / len(structures), 4)
                                   if structures else None),
        "failed": [r["taskId"] for r in runs if not r.get("ok")],
    }
    return {"profile": "full", "allLaw": True, "runs": runs, "summary": summary,
            "ok": bool(runs) and not summary["failed"]}


def _run_repeated(args: argparse.Namespace, reference: Path | None) -> dict[str, Any]:
    """连跑 N 次并聚合中位数/最差值/方差（上线门槛的「连跑 3 次」口径）。"""
    from .driver import resolve_reference, run_full_profile
    from .harness import aggregate_runs

    base = Path(args.data_dir)
    task_reference = resolve_reference(base, args.task) or reference
    from .. import use_device_profile

    use_device_profile()
    runs: list[dict[str, Any]] = []
    for index in range(args.repeat):
        run = run_full_profile(
            base, task_reference, out=None,
            runtime=args.runtime,
            collect_harness_metrics=args.collect_harness_metrics,
            fault=args.inject_fault,
            interrupt_at=args.interrupt_at,
            task_filter=args.task,
            # 每次重复必须是干净工作空间：否则上一次的 submission 会被
            # 下一次当成本次已答内容读入账本，重复提交与分数都会被污染。
            workspace_dir=(Path(args.workspace) / f"{args.task or 'default'}-r{index + 1}"
                           if args.workspace else None),
        )
        run["runIndex"] = index + 1
        runs.append(run)
    aggregate = aggregate_runs([
        {
            "branch": r.get("branch"),
            "merge": r.get("merge"),
            "composite": r.get("composite"),
            "delivery": (r.get("harness") or {}).get("delivery"),
            "units": (r.get("harness") or {}).get("units"),
        }
        for r in runs
    ])
    return {
        "profile": "full",
        "runtime": args.runtime,
        "repeat": args.repeat,
        "ok": all(r.get("ok") for r in runs),
        "runs": runs,
        "aggregate": aggregate,
    }


if __name__ == "__main__":
    raise SystemExit(main())
