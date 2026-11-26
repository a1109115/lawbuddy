"""Harness 指标采集与故障注入（LB-HARNESS-2026-01 §4.9 / B-06）。

本模块只做**观测**与**故障注入**，不参与评分口径：
- `collect_metrics`：从工作账本、预算表与事件流汇总 Harness 指标（结构交付率、
  工作单元终态率、文档覆盖率、预算恢复与重复提交、Token/提交格、重复读取率、
  重复检索率），全部可由原始日志复算；
- `aggregate_runs`：多次运行的中位数/最差值/方差；
- `FaultInjector`：按 429 / timeout / disconnect 注入模型网关故障，验证不产生
  静默空批次与重复副作用。

所有指标只含 ID、数量、状态与错误码，不含案件原文、完整引文与密钥。
"""

from __future__ import annotations

import statistics
from typing import Any, Callable

FAULT_CODES = ("429", "timeout", "disconnect")


def collect_metrics(task_id: str, *, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """汇总单个任务的 Harness 指标（账本/预算缺失时以 0 或 null 表示，不臆造）。"""
    from lawbuddy.agent import budget, scheduler, work_ledger

    metrics: dict[str, Any] = {
        "taskId": task_id,
        "units": None,
        "cells": None,
        "delivery": None,
        "budget": None,
        "runtime": None,
        "tokensPerCommittedCell": None,
        "duplicateReadRate": None,
        "duplicateSearchRate": None,
        "documentCoverage": None,
        "recovered": False,
        "redundantCommits": 0,
    }
    try:
        progress = scheduler.progress(task_id)
        metrics["units"] = progress["units"]
        metrics["cells"] = progress["cells"]
        metrics["delivery"] = progress["delivery"]
    except Exception:  # noqa: BLE001 账本未启用时保持 null
        pass
    try:
        snap = budget.snapshot(task_id)
        metrics["budget"] = {
            "stage": snap.get("stage"),
            "limits": snap.get("limits") or {},
            # 只保留数值计数，不把 `calls` 里的工具参数摘要写进评测报告
            "used": budget.public_used(snap.get("used")),
            "reasons": snap.get("reasons") or [],
            "noProgressRounds": snap.get("noProgressRounds", 0),
        }
    except Exception:  # noqa: BLE001
        pass
    runtime = work_ledger.get_runtime(task_id)
    if runtime:
        metrics["runtime"] = {
            "runtimeVersion": runtime["runtimeVersion"],
            "roundsUsed": runtime["roundsUsed"],
            "toolCallsUsed": runtime["toolCallsUsed"],
            "modelCallsUsed": runtime["modelCallsUsed"],
            "promptTokens": runtime["promptTokens"],
            "completionTokens": runtime["completionTokens"],
        }
        committed = int((metrics["cells"] or {}).get("committed") or 0)
        if committed:
            metrics["tokensPerCommittedCell"] = round(
                runtime["promptTokens"] / committed, 2)

    if events:
        metrics["recovered"] = any(e.get("type") == "run_recovered" for e in events)
        metrics["redundantCommits"] = _count_redundant_commits(events)
        metrics["duplicateReadRate"], metrics["duplicateSearchRate"] = _efficiency_rates(events)
        metrics["documentCoverage"] = _document_coverage(events)
    return metrics


def _count_redundant_commits(events: list[dict[str, Any]]) -> int:
    """重复提交次数：同一交付物同一状态下重复出现的 artifact_commit 数量。

    正常实现每次原子提交都会推进状态或行数；状态与行数完全相同的重复提交记
    冗余（幂等本身正确，但说明存在重复写入路径）。
    """
    seen: dict[str, tuple[str, int]] = {}
    redundant = 0
    for event in events:
        if event.get("type") != "artifact_commit":
            continue
        payload = event.get("payload") or {}
        name = str(payload.get("name") or "")
        signature = (str(payload.get("status") or ""), int(payload.get("rows") or 0))
        if seen.get(name) == signature:
            redundant += 1
        seen[name] = signature
    return redundant


def _efficiency_rates(events: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    """（重复读取率, 重复检索率）= 重复次数 / 每百次相关调用。"""
    duplicate_reads = duplicate_searches = reads = searches = 0
    for event in events:
        if event.get("type") != "turn_complete":
            continue
        eff = (event.get("payload") or {}).get("efficiency") or {}
        # 键名与 autonomous._efficiency_summary 一致：reads / searches 是分母，
        # repeatedReads / duplicateSearches 是分子。读错键会让「重复读取率」
        # 恒为 null，C-06 的效率指标就无法复算。
        duplicate_reads += int(eff.get("repeatedReads") or 0)
        duplicate_searches += int(eff.get("duplicateSearches") or 0)
        reads += int(eff.get("reads") or 0)
        searches += int(eff.get("searches") or 0)
    read_rate = round(duplicate_reads / reads * 100, 2) if reads else None
    search_rate = round(duplicate_searches / searches * 100, 2) if searches else None
    return read_rate, search_rate


def _document_coverage(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """文档覆盖率：从 turn_complete 的阅读覆盖字段汇总（无该字段时为 null）。"""
    for event in reversed(events):
        if event.get("type") != "turn_complete":
            continue
        coverage = ((event.get("payload") or {}).get("finalize") or {}).get("coverage")
        if isinstance(coverage, dict):
            return coverage
    return None


def aggregate_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """多次运行聚合：中位数/最差值/方差（对应上线门槛的「连跑 3 次」口径）。"""
    def _series(key: str) -> list[float]:
        out: list[float] = []
        for run in runs:
            value = run.get(key)
            if isinstance(value, (int, float)):
                out.append(float(value))
        return out

    result: dict[str, Any] = {"runs": len(runs)}
    for key in ("branch", "merge", "composite"):
        series = _series(key)
        if not series:
            continue
        result[key] = {
            "median": round(statistics.median(series), 4),
            "min": round(min(series), 4),
            "max": round(max(series), 4),
            "stdev": round(statistics.pstdev(series), 4) if len(series) > 1 else 0.0,
        }
    # 结构交付率只看结构完整性：未终态答案格会让 status 变 partial，
    # 但「有没有结构交付物」是另一个维度（方案 §1.1-7）。
    structures = [1.0 if (run.get("delivery") or {}).get("structuralStatus") == "complete"
                  else 0.0 for run in runs if run.get("delivery")]
    if structures:
        result["structuralDeliveryRate"] = round(sum(structures) / len(structures), 4)
    terminals = [(run.get("units") or {}).get("terminalRatio") for run in runs
                 if (run.get("units") or {}).get("terminalRatio") is not None]
    if terminals:
        result["unitTerminalRate"] = round(sum(terminals) / len(terminals), 4)
    return result


class FaultInjector:
    """模型网关故障注入（429 / timeout / disconnect）。

    `wrap(fn)` 返回一个包装后的调用：第 `at_call` 次调用触发一次故障，
    之后恢复正常——用于验证「首字前断连 / 正文断连 / 429」都不产生静默空批次。
    """

    def __init__(self, fault: str, at_call: int = 1) -> None:
        if fault not in FAULT_CODES:
            raise ValueError(f"未知故障类型：{fault}（可用：{', '.join(FAULT_CODES)}）")
        self.fault = fault
        self.at_call = max(1, int(at_call))
        self.calls = 0
        self.injected = 0

    def wrap(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        def inner(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == self.at_call:
                self.injected += 1
                from lawbuddy.agent.model_gateway import ModelGatewayError, StreamInterrupted

                if self.fault == "429":
                    raise ModelGatewayError("注入故障：429 限流（rate limited）")
                if self.fault == "timeout":
                    raise StreamInterrupted("注入故障：请求超时（正文断连）")
                raise StreamInterrupted("注入故障：连接中断（RemoteDisconnected）")
            return fn(*args, **kwargs)

        return inner

    def report(self) -> dict[str, Any]:
        return {"fault": self.fault, "atCall": self.at_call,
                "calls": self.calls, "injected": self.injected}
