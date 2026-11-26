"""评测用最小任务镜像（P2-12 快速档）。

不依赖外部数据快照：3 份协议 × 3 个交付点 + 3 道聚合题，覆盖单选、多选与
三种聚合算子（count / list / distribution）。用它验证评分与聚合口径的确定性，
使 CI 在没有真实卷宗与 API Key 时也能守住"改坏评分口径"的回归。
"""

from __future__ import annotations

from typing import Any

AGREEMENTS = ["A01", "A02", "A03"]

# 交付点 → 选项全集；multi 标记多选（答案用 " | " 连接）
OPTIONS: dict[str, list[str]] = {
    "mae_taken_as_whole": ["Yes", "No"],
    "mae_co_economic": ["Yes", "No"],
    "mae_co_any_person": ["Buyer", "Target", "Any Person"],
}

MULTI: dict[str, bool] = {
    "mae_taken_as_whole": False,
    "mae_co_economic": True,
    "mae_co_any_person": False,
}

# 真值表（与官方 deal_points.csv 同构：agreement, deal_point, answer）
GOLD_TABLE: dict[tuple[str, str], str] = {
    ("A01", "mae_taken_as_whole"): "Yes",
    ("A02", "mae_taken_as_whole"): "No",
    ("A03", "mae_taken_as_whole"): "Yes",
    ("A01", "mae_co_economic"): "Yes | No",
    ("A02", "mae_co_economic"): "Yes",
    ("A03", "mae_co_economic"): "Yes | No",
    ("A01", "mae_co_any_person"): "Buyer",
    ("A02", "mae_co_any_person"): "Target",
    ("A03", "mae_co_any_person"): "Buyer",
}

# 聚合题规格（结构对齐官方 merge_spec.json）
MERGE_SPEC: list[dict[str, Any]] = [
    {
        "id": "M01",
        "op": "count",
        "cond": {"dp": "mae_taken_as_whole", "eq": "Yes"},
    },
    {
        "id": "M02",
        "op": "list",
        "cond": {"dp": "mae_co_economic", "has": "No"},
    },
    {
        "id": "M03",
        "op": "distribution",
        "dp": "mae_co_any_person",
        "options": ["Buyer", "Target", "Any Person"],
    },
]

GOLD_ANSWERS: dict[str, Any] = {
    "M01": 2,
    "M02": ["A01", "A03"],
    "M03": {"Buyer": 2, "Target": 1, "Any Person": 0},
}


def build_mini_fixture() -> dict[str, Any]:
    """返回快速档用的任务镜像（表格真值 + 聚合规格 + 选项元数据）。"""
    return {
        "name": "wfa-law-mini",
        "agreements": list(AGREEMENTS),
        "dealPoints": list(OPTIONS),
        "options": {k: list(v) for k, v in OPTIONS.items()},
        "multi": dict(MULTI),
        "table": dict(GOLD_TABLE),
        "mergeSpec": [dict(q) for q in MERGE_SPEC],
        "goldAnswers": dict(GOLD_ANSWERS),
    }


# ---------------------------------------------------------------------------
# 代表任务 2：刑事多被告研判（12 被告分批 + 结构化合并 + 逐被告判定）
#
# 快速档要求"2 个代表任务"（§4.9）。第 1 个是上面的合同 deal-point 镜像，
# 第 2 个取刑事侧最具压力的一类：多被告、需分批生成、按被告合并、逐人判定
# 罪名成立与否。本镜像给出判决真值与评分口径，用于验证：
# - 分批生成后合并的实体齐全性（人数不丢）
# - 逐被告判定（单选精确匹配）与角色/罪名口径一致
# ---------------------------------------------------------------------------

CRIM_DEFENDANTS = [
    "D01", "D02", "D03", "D04", "D05", "D06",
    "D07", "D08", "D09", "D10", "D11", "D12",
]

# 每个被告的罪名集合（真值）：用于 roles/charges 键集合一致性校验
CRIM_CHARGES: dict[str, list[str]] = {
    "D01": ["诈骗罪", "伪造金融票证罪"],
    "D02": ["诈骗罪"],
    "D03": ["诈骗罪", "掩饰隐瞒犯罪所得罪"],
    "D04": ["诈骗罪"],
    "D05": ["诈骗罪", "合同诈骗罪"],
    "D06": ["合同诈骗罪"],
    "D07": ["诈骗罪"],
    "D08": ["诈骗罪", "伪造公司印章罪"],
    "D09": ["掩饰隐瞒犯罪所得罪"],
    "D10": ["合同诈骗罪"],
    "D11": ["诈骗罪"],
    "D12": ["诈骗罪"],
}

# 逐被告判定真值：是否构成犯罪（单选 Yes/No）
CRIM_VERDICT: dict[str, str] = {
    "D01": "Yes", "D02": "Yes", "D03": "Yes", "D04": "No",
    "D05": "Yes", "D06": "Yes", "D07": "Yes", "D08": "Yes",
    "D09": "Yes", "D10": "No", "D11": "Yes", "D12": "Yes",
}

# 刑事侧聚合题（与官方 merge 同构）
CRIM_MERGE_SPEC: list[dict[str, Any]] = [
    {
        "id": "C01",
        "op": "count",
        "cond": {"dp": "verdict", "eq": "Yes"},
    },
    {
        "id": "C02",
        "op": "list",
        "cond": {"dp": "verdict", "eq": "No"},
    },
    {
        "id": "C03",
        "op": "distribution",
        "dp": "verdict",
        "options": ["Yes", "No"],
    },
]

CRIM_GOLD_ANSWERS: dict[str, Any] = {
    "C01": 10,
    "C02": ["D04", "D10"],
    "C03": {"Yes": 10, "No": 2},
}


def build_criminal_fixture() -> dict[str, Any]:
    """代表任务 2：12 被告刑事研判镜像（含分批规划所需的实体全集）。"""
    table = {(d, "verdict"): v for d, v in CRIM_VERDICT.items()}
    return {
        "name": "wfa-law-criminal",
        "agreements": list(CRIM_DEFENDANTS),
        "dealPoints": ["verdict"],
        "options": {"verdict": ["Yes", "No"]},
        "multi": {"verdict": False},
        "table": table,
        "mergeSpec": [dict(q) for q in CRIM_MERGE_SPEC],
        "goldAnswers": dict(CRIM_GOLD_ANSWERS),
        "expectedEntities": list(CRIM_DEFENDANTS),
        "charges": {k: list(v) for k, v in CRIM_CHARGES.items()},
    }


def build_fast_fixtures() -> list[dict[str, Any]]:
    """快速档的两个代表任务（合同 deal-point + 刑事多被告）。"""
    return [build_mini_fixture(), build_criminal_fixture()]
