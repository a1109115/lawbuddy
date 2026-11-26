"""结果报告与回归 diff（P2-12）。

同一 commit 两次运行的评分必须完全一致；与历史结果对比时输出可读 diff，
让"分数下降"在 CI 上直接可见，而不是靠人工翻日志。
"""

from __future__ import annotations

from typing import Any

# 对比时的关键指标（顺序即报告顺序）
KEYS = ("branch", "merge", "merge_partial", "full_success", "n_cells", "n_merge_questions")


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def compare_reports(base: dict[str, Any], new: dict[str, Any],
                    tolerance: float = 0.0) -> dict[str, Any]:
    """对比两次运行的结果，返回差异项与是否通过。

    通过条件：关键指标不劣化（branch/merge 下降超过 tolerance 即判失败），
    且 full_success 不能从 True 变 False。
    """
    diffs: list[dict[str, Any]] = []
    failed = False

    def walk(prefix: str, b: Any, n: Any) -> None:
        nonlocal failed
        if isinstance(b, dict) and isinstance(n, dict):
            for key in sorted(set(b) | set(n)):
                walk(f"{prefix}.{key}" if prefix else str(key), b.get(key), n.get(key))
            return
        bn, nn = _num(b), _num(n)
        if bn is not None and nn is not None:
            delta = nn - bn
            if abs(delta) > tolerance:
                diffs.append({"key": prefix, "base": bn, "new": nn, "delta": round(delta, 4)})
                if prefix in ("branch", "merge", "merge_partial") and delta < -tolerance:
                    failed = True
                if prefix == "full_success" and bn > 0 and nn == 0:
                    failed = True
        elif b != n:
            diffs.append({"key": prefix, "base": b, "new": n, "delta": None})

    walk("", {k: base.get(k) for k in KEYS if k in base}, {k: new.get(k) for k in KEYS if k in new})
    return {
        "ok": not failed,
        "keys": list(KEYS),
        "diffs": diffs,
        "summary": "无差异" if not diffs else f"{len(diffs)} 项变化"
                   + ("（存在劣化）" if failed else "（均为提升或持平）"),
    }


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    """从评测结果里提取报告所需的核心指标（供 CI 与历史对比使用）。"""
    out = {k: result.get(k) for k in KEYS}
    for extra in ("scorer", "submission_error", "profile"):
        if extra in result:
            out[extra] = result[extra]
    return out
