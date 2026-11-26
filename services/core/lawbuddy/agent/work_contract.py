"""任务契约编译器（LB-HARNESS-2026-01 §4.2 / B-01）。

把「用户声明的交付契约 + 材料元数据」编译成确定的**工作全集**：

- 文件 Schema、字段、主键、合法值、多选规则；
- 答案格与工作单元：支持两种真实布局
  * `entity_question_row`（长表）：每行一个「实体 × 问题」，答案是单独一列
    （`deal_points.csv`：agreement + deal_point → answer，20×15 = 300 行）；
  * `entity_field_matrix`（矩阵）：每行一个实体，字段是列（13 被告 × 各字段）；
  * `document_question` / `document_field`：按材料分片；
  * `artifact`：未声明覆盖结构时的整体物化单元；
- 派生产物及其对主交付物的聚合依赖；
- 行覆盖 / 证据 / 值域 / 版本 / 派生一致性五类验收规则。

本模块是**纯结构编译器**：只描述「必须覆盖哪些格子、怎么判断覆盖完成」，
不生成、不推断任何法律结论或事实答案。合法值域只在任务显式声明时使用，
不按合同惯例或模型常识补全。同一输入重复编译结果逐字节一致，因此重复编译
不会产生重复工作单元（HAR-T-001）。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

CONTRACT_VERSION = 1

# 依赖令牌：`artifact:<name>` 表示该交付物全部答案格进入终态后才可物化
DEP_UNIT = "unit:"
DEP_ARTIFACT = "artifact:"

# 优先级（数值越小越先调度）：解除他人阻塞 → 未尝试 → 待修复 → 可重试失败
PRIORITY_UNBLOCK = 10
PRIORITY_FRESH = 100
PRIORITY_REPAIR = 300
PRIORITY_RETRY = 500

# 长表布局下的行主键分隔符（与 CSV 主键列取值拼接方式一致）
ROW_SEP = "::"

# 来源列（方案 §2.1 第 2 条）：只证明答案出处，不是待填答案，不参与布局判定。
# 命名与 delivery_controller 的取数键一致，便于「写文件 → 同步答案格」直接对齐。
SOURCE_COLUMNS = ("documentId", "versionId", "pageNo", "quote")


def _slug(raw: Any, fallback: str = "item") -> str:
    """稳定标识：保留中文与字母数字，其余压成单个下划线。"""
    text = str(raw or "").strip()
    text = re.sub(r"[^\w\u4e00-\u9fa5.-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_.-")
    if not text:
        return fallback
    return text[:96]


def _key_text(raw: Any) -> str:
    """行/格标识中的键值：只压空白，不改动原始字符（要与 CSV 实际取值对齐）。"""
    return " ".join(str(raw or "").split())


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _unique(base: str, used: set[str]) -> str:
    """同名冲突时追加稳定短哈希（同一输入必得同一结果）。"""
    if base not in used:
        used.add(base)
        return base
    candidate = f"{base}~{_digest(base)}"
    index = 2
    while candidate in used:
        candidate = f"{base}~{_digest(base)}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _columns(spec: dict[str, Any]) -> list[str]:
    return [str(c) for c in (spec.get("columns") or []) if str(c or "").strip()]


def _key_columns(spec: dict[str, Any]) -> list[str]:
    return [str(c) for c in (spec.get("key") or []) if str(c or "").strip()]


def _fill_columns(spec: dict[str, Any]) -> list[str]:
    """需要「填值」的列 = 全部列 − 主键列 − 来源列（主键与来源都不是答案）。

    来源列（文档/版本/页码/短引）只证明答案出处，不是待填答案。若不排除，
    一份「1 个答案列 + 3 个来源列」的长表会被判成 4 个填值列，布局判定退化为
    按实体聚合（16 行而不是 288 行），答案格与行覆盖全部失真。
    """
    key = set(_key_columns(spec))
    return [c for c in _columns(spec) if c not in key and c not in SOURCE_COLUMNS]


def _questions_of(spec: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for q in spec.get("questions") or []:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id") or "").strip()
        text = str(q.get("text") or "").strip()
        if qid and text:
            out.append({"id": qid, "text": text})
    return out


def _entities_of(spec: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ent in spec.get("expectedEntities") or []:
        if not isinstance(ent, dict):
            continue
        eid = str(ent.get("id") or "").strip()
        if not eid:
            continue
        out.append({
            "id": eid,
            "label": str(ent.get("label") or eid).strip(),
            "fields": [str(f) for f in (ent.get("fields") or []) if str(f or "").strip()],
        })
    return out


def _row_key(entity_id: str, field_id: str) -> str:
    return f"{_key_text(entity_id)}{ROW_SEP}{_key_text(field_id)}"


def _unit(
    unit_id: str,
    scope_type: str,
    scope: dict[str, Any],
    *,
    priority: int = PRIORITY_FRESH,
    dependencies: list[str] | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    return {
        "workUnitId": unit_id,
        "scopeType": scope_type,
        "scope": scope,
        "dependencies": dependencies or [],
        "priority": priority,
        "maxAttempts": max_attempts,
    }


def _doc_sources(documents: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """材料元数据规范化（只留标识、标题、页数、活动版本，不含正文）。"""
    out: list[dict[str, Any]] = []
    for doc in documents or []:
        if not isinstance(doc, dict):
            continue
        doc_id = str(doc.get("id") or doc.get("documentId") or "").strip()
        if not doc_id:
            continue
        out.append({
            "documentId": doc_id,
            "title": str(doc.get("title") or "")[:200],
            "pageCount": int(doc.get("pageCount") or 0),
            "versionId": str(doc.get("activeVersionId") or doc.get("versionId") or "") or None,
        })
    out.sort(key=lambda d: d["documentId"])
    return out


def _layout(
    entities: list[dict[str, Any]],
    questions: list[dict[str, str]],
    fill_columns: list[str],
    key_columns: list[str],
    docs: list[dict[str, Any]],
    fmt: str,
) -> str:
    """判定交付物布局（确定性，只看声明不猜业务）：

    - 有实体 + 有多个主键列 + 只有一个填值列 → 长表（每行一个实体×问题）；
    - 有实体 → 矩阵（实体为行，字段/问题为列）；
    - JSON + 只有问题、没有实体 → 扁平键图（题号 → 答案，如 answers.json）；
    - 有问题 + 有材料 → 文档×问题；
    - 有材料 + 有填值列 + CSV → 文档×列；
    - 否则整体物化。
    """
    if entities and questions and len(key_columns) >= 2 and len(fill_columns) == 1:
        return "entity_question_row"
    if entities:
        return "entity_field_matrix"
    # 扁平键图必须先于 document_question 判定：answers.json 这类「题号 → 答案」的
    # 交付物没有材料维度，若落到 document_question 会编译出「16 份材料 × 17 题」
    # 共 272 个并不存在的格子，覆盖校验与终态率随之失真。
    if questions and fmt == "json":
        return "question_keys"
    if questions and docs:
        return "document_question"
    if docs and fill_columns:
        return "document_field"
    return "artifact"


def compile_artifact(
    spec: dict[str, Any],
    documents: list[dict[str, Any]] | None = None,
    *,
    used_unit_ids: set[str] | None = None,
) -> dict[str, Any]:
    """编译单个交付物：工作单元 + 答案格 + 验收规则。"""
    name = str(spec.get("name") or "").strip()
    used = used_unit_ids if used_unit_ids is not None else set()
    fmt = str(spec.get("format") or "md")
    entities = _entities_of(spec)
    questions = _questions_of(spec)
    docs = _doc_sources(documents)
    columns = _columns(spec)
    key_columns = _key_columns(spec)
    fill_columns = _fill_columns(spec)
    multi = [str(m) for m in (spec.get("multiPoints") or []) if str(m or "").strip()]
    valid_values = {
        str(k): [str(v) for v in vals if str(v or "").strip()]
        for k, vals in (spec.get("validValues") or {}).items()
        if isinstance(vals, list)
    }
    layout = _layout(entities, questions, fill_columns, key_columns, docs, fmt)

    units: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    row_keys: list[str] = []
    value_column = ""

    if layout == "entity_question_row":
        # 长表：20 协议 × 15 deal point = 300 行，每行单独一格
        value_column = fill_columns[0]
        for ent in entities:
            for q in questions:
                rkey = _row_key(ent["id"], q["id"])
                row_keys.append(rkey)
                unit_id = _unique(
                    f"{_slug(name)}:{_slug(ent['id'])}:{_slug(q['id'])}", used)
                units.append(_unit(unit_id, "entity_question", {
                    "artifact": name,
                    "layout": layout,
                    "entityId": ent["id"],
                    "entityLabel": ent["label"],
                    "questionId": q["id"],
                    "questionText": q["text"],
                    # 长表行的格标识就是行标识；缺了 cellKey，答案格回写到工作单元时
                    # 取不到该单元（sync_units_for_artifact 按 scope.cellKey 匹配），
                    # 全部单元会永远停在 pending。
                    "cellKey": rkey,
                    "rowKey": rkey,
                    "valueColumn": value_column,
                    "multiPoint": q["id"] in multi or q["text"] in multi,
                }))
                cells.append({
                    "artifact": name,
                    "cellKey": rkey,
                    "rowKey": rkey,
                    "entityId": ent["id"],
                    "fieldId": q["id"],
                    "valueColumn": value_column,
                })
    elif layout == "entity_field_matrix":
        # 矩阵：13 被告为行，字段为列（列名即字段名）
        field_source: list[tuple[str, str]] = []
        if questions:
            field_source = [(q["id"], q["text"]) for q in questions]
        elif fill_columns:
            field_source = [(c, c) for c in fill_columns]
        for ent in entities:
            row_keys.append(_key_text(ent["id"]))
            ent_fields = [f for f in ent["fields"] if f]
            fields = [(f, f) for f in ent_fields] if ent_fields else field_source
            for field_id, field_text in fields:
                ckey = _row_key(ent["id"], field_id)
                unit_id = _unique(
                    f"{_slug(name)}:{_slug(ent['id'])}:{_slug(field_id)}", used)
                units.append(_unit(unit_id, "entity_field", {
                    "artifact": name,
                    "layout": layout,
                    "entityId": ent["id"],
                    "entityLabel": ent["label"],
                    "fieldId": field_id,
                    "fieldText": field_text,
                    "rowKey": _key_text(ent["id"]),
                    "cellKey": ckey,
                    "multiPoint": field_id in multi or field_text in multi,
                }))
                cells.append({
                    "artifact": name,
                    "cellKey": ckey,
                    "rowKey": _key_text(ent["id"]),
                    "entityId": ent["id"],
                    "fieldId": field_id,
                    "valueColumn": field_id,
                })
    elif layout == "question_keys":
        # 扁平键图（M01…M17）：一个题号一格，没有材料维度与字段层。
        for q in questions:
            ckey = _key_text(q["id"])
            row_keys.append(ckey)
            unit_id = _unique(f"{_slug(name)}:{_slug(q['id'])}", used)
            units.append(_unit(unit_id, "question_key", {
                "artifact": name,
                "layout": layout,
                "questionId": q["id"],
                "questionText": q["text"],
                "rowKey": ckey,
                "cellKey": ckey,
                "multiPoint": q["id"] in multi or q["text"] in multi,
            }))
            cells.append({
                "artifact": name,
                "cellKey": ckey,
                "rowKey": ckey,
                "entityId": q["id"],
                "fieldId": "",
                "valueColumn": "",
            })
    elif layout == "document_question":
        for doc in docs:
            row_keys.append(_key_text(doc["documentId"]))
            for q in questions:
                ckey = _row_key(doc["documentId"], q["id"])
                unit_id = _unique(
                    f"{_slug(name)}:{_slug(doc['documentId'])}:{_slug(q['id'])}", used)
                units.append(_unit(unit_id, "document_question", {
                    "artifact": name,
                    "layout": layout,
                    "documentId": doc["documentId"],
                    "documentTitle": doc["title"],
                    "versionId": doc["versionId"],
                    "questionId": q["id"],
                    "questionText": q["text"],
                    "rowKey": _key_text(doc["documentId"]),
                    "cellKey": ckey,
                    "multiPoint": q["id"] in multi or q["text"] in multi,
                }))
                cells.append({
                    "artifact": name,
                    "cellKey": ckey,
                    "rowKey": _key_text(doc["documentId"]),
                    "documentId": doc["documentId"],
                    "fieldId": q["id"],
                    "valueColumn": fill_columns[0] if fill_columns else "",
                })
    elif layout == "document_field":
        for doc in docs:
            row_keys.append(_key_text(doc["documentId"]))
            for col in fill_columns:
                ckey = _row_key(doc["documentId"], col)
                unit_id = _unique(
                    f"{_slug(name)}:{_slug(doc['documentId'])}:{_slug(col)}", used)
                units.append(_unit(unit_id, "document_field", {
                    "artifact": name,
                    "layout": layout,
                    "documentId": doc["documentId"],
                    "documentTitle": doc["title"],
                    "versionId": doc["versionId"],
                    "fieldId": col,
                    "rowKey": _key_text(doc["documentId"]),
                    "cellKey": ckey,
                    "multiPoint": col in multi,
                }))
                cells.append({
                    "artifact": name,
                    "cellKey": ckey,
                    "rowKey": _key_text(doc["documentId"]),
                    "documentId": doc["documentId"],
                    "fieldId": col,
                    "valueColumn": col,
                })
    else:
        unit_id = _unique(f"{_slug(name)}:materialize", used)
        units.append(_unit(unit_id, "artifact", {"artifact": name, "format": fmt}))

    # 物化单元：把答案格确定性写入正式交付物；依赖该交付物全部答案格进入终态
    if cells:
        materialize_id = _unique(f"{_slug(name)}:materialize", used)
        units.append(_unit(
            materialize_id, "materialize",
            {"artifact": name, "layout": layout, "rowsExpected": len(row_keys)},
            priority=PRIORITY_UNBLOCK,
            dependencies=[DEP_ARTIFACT + name],
        ))

    derived_units: list[dict[str, Any]] = []
    derived_specs: list[dict[str, Any]] = []
    for derived in spec.get("derivedArtifacts") or []:
        if not isinstance(derived, dict):
            continue
        dname = str(derived.get("name") or "").strip()
        if not dname:
            continue
        sources = [str(s) for s in (derived.get("dependsOn") or []) if str(s or "").strip()]
        group_by = [str(g) for g in (derived.get("groupBy") or []) if str(g or "").strip()]
        tally = [str(c) for c in (derived.get("columns") or []) if str(c or "").strip()]
        derived_specs.append({"name": dname, "dependsOn": sources,
                              "groupBy": group_by, "columns": tally})
        unit_id = _unique(f"{_slug(dname)}:aggregate", used)
        derived_units.append(_unit(
            unit_id, "derive",
            {
                "artifact": dname,
                "dependsOn": sources or [name],
                "description": str(derived.get("description") or "").strip(),
                "groupBy": group_by,
                "columns": tally,
            },
            priority=PRIORITY_UNBLOCK,
            dependencies=[DEP_ARTIFACT + s for s in (sources or [name])],
        ))

    rules: list[dict[str, Any]] = [
        {"kind": "file_present", "artifact": name, "format": fmt},
    ]
    if cells:
        rules.append({
            "kind": "row_coverage",
            "artifact": name,
            "layout": layout,
            "rowsExpected": len(row_keys),
            "rowKeys": row_keys,
            "cellsExpected": len(cells),
        })
        rules.append({"kind": "evidence", "artifact": name,
                      "requireSource": True, "allowNeedsHuman": True})
        rules.append({"kind": "version", "artifact": name})
    for column, allowed in valid_values.items():
        rules.append({
            "kind": "value_domain",
            "artifact": name,
            "column": column,
            "allowed": allowed,
        })

    return {
        "name": name,
        "format": fmt,
        "layout": layout,
        "columns": columns,
        "key": key_columns,
        "valueColumn": value_column,
        "multiPoints": multi,
        "validValues": valid_values,
        "questions": questions,
        "entities": entities,
        "rowKeys": row_keys,
        "workUnits": units + derived_units,
        "cells": cells,
        "rules": rules,
        "derived": [
            {"name": str(d.get("name")),
             "dependsOn": [str(s) for s in (d.get("dependsOn") or [])]}
            for d in (spec.get("derivedArtifacts") or [])
            if isinstance(d, dict) and d.get("name")
        ],
        "derivedSpecs": derived_specs,
        "skeleton": spec.get("skeleton") if fmt == "json" else None,
        # JSON 实体容器（如 [\"defendants\"]）：交付控制器据此取实体集合校验覆盖。
        "path": [str(p) for p in (spec.get("path") or []) if str(p or "").strip()],
        "itemKey": str(spec.get("itemKey") or ""),
    }


def compile_contract(
    specs: list[dict[str, Any]],
    documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """编译整个任务的交付契约（确定性：同一输入重复编译结果一致）。"""
    used: set[str] = set()
    artifacts: list[dict[str, Any]] = []
    work_units: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    for spec in specs or []:
        if not isinstance(spec, dict) or not str(spec.get("name") or "").strip():
            continue
        compiled = compile_artifact(spec, documents, used_unit_ids=used)
        artifacts.append({k: compiled[k] for k in
                          ("name", "format", "layout", "columns", "key", "valueColumn",
                           "multiPoints", "questions", "entities", "rowKeys",
                           "derived", "derivedSpecs", "skeleton")})
        # JSON 交付物的实体容器路径（如 judgment.json 的 defendants）必须下传：
        # 交付控制器据此取实体集合做覆盖校验，否则只能退化为"文件非空"。
        for extra in ("path", "itemKey"):
            if compiled.get(extra):
                artifacts[-1][extra] = compiled[extra]
        work_units.extend(compiled["workUnits"])
        cells.extend(compiled["cells"])
        rules.extend(compiled["rules"])
    for art in artifacts:
        for derived in art["derived"]:
            rules.append({
                "kind": "derived_consistency",
                "artifact": derived["name"],
                "dependsOn": derived["dependsOn"] or [art["name"]],
            })
    contract = {
        "version": CONTRACT_VERSION,
        "artifacts": artifacts,
        "workUnits": work_units,
        "cells": cells,
        "rules": rules,
        "summary": {
            "artifacts": len(artifacts),
            "workUnits": len(work_units),
            "cells": len(cells),
            "rows": sum(len(a["rowKeys"]) for a in artifacts),
            "derivedArtifacts": sum(len(a["derived"]) for a in artifacts),
        },
    }
    contract["fingerprint"] = fingerprint(contract)
    return contract


def fingerprint(contract: dict[str, Any]) -> str:
    """契约指纹（不含时间戳）：用于判断是否需要重新编译 / 迁移工作全集。"""
    payload = {
        "version": contract.get("version"),
        "artifacts": [
            {k: a.get(k) for k in ("name", "layout", "columns", "key", "rowKeys")}
            for a in (contract.get("artifacts") or [])
        ],
        "workUnits": [u.get("workUnitId") for u in (contract.get("workUnits") or [])],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def units_by_id(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(u["workUnitId"]): u
        for u in (contract.get("workUnits") or [])
        if u.get("workUnitId")
    }


def artifacts_by_name(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(a["name"]): a
        for a in (contract.get("artifacts") or [])
        if a.get("name")
    }
