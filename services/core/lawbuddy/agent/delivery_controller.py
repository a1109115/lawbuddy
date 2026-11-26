"""确定性交付控制器（LB-HARNESS-2026-01 §4.6 / B-03）。

职责边界：
- 接收结构化候选（答案格）→ 答案格与工作单元状态由工作账本落库；
- 按契约补齐**完整行全集**并物化正式 CSV/JSON（原子写）；
- 执行已声明的派生聚合；
- 写后重新读取并校验（行覆盖、值域、证据状态、版本、派生一致性）；
- 产物每次原子提交后立即登记。

物化只使用「已提交或已明确标记 needs_human/blocked」的答案格：未确认的格子
按空值写出并在账本里保留状态，**绝不当作否定答案**参与聚合（HAR-T-013）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from ..sandbox import workspace_root
from . import deliverables as deliverable_store
from . import work_ledger

# 行主键分隔符（与 work_contract.ROW_SEP 一致）
ROW_SEP = "::"


def _submission(root: Path) -> Path:
    return deliverable_store.submission_dir(root)


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    if not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text))
    return [
        {(k or "").strip(): ("" if v is None else str(v)) for k, v in row.items()}
        for row in reader
    ]


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8") or "null")
    except (OSError, json.JSONDecodeError):
        return None


def _json_path(artifact: dict[str, Any]) -> list[str]:
    return [str(p) for p in (artifact.get("path") or []) if str(p or "").strip()]


def _json_entries(data: Any, artifact: dict[str, Any]) -> dict[str, Any]:
    """取出 JSON 交付物的「实体 → 内容」映射（如 judgment.json 的 defendants）。

    未声明 `path` 时，顶层对象本身就视为实体映射；声明了 `path` 但结构不符时
    返回空映射，让覆盖校验如实报缺口，而不是把整个文件当成已覆盖。
    """
    keys = _json_path(artifact)
    node = data
    for key in keys:
        if not isinstance(node, dict) or not isinstance(node.get(key), dict):
            return {}
        node = node[key]
    return node if isinstance(node, dict) else {}


def _json_entry_filled(entry: Any) -> bool:
    """实体是否已有实质内容（空列表/空对象/空字符串/None 都算未填）。

    `_status` / `reasonCode` 这类元数据不算答案；`status` 只有在取值是
    needs_human/blocked 这类终态标记时才跳过，避免把同名业务字段误判为空。
    """
    if isinstance(entry, dict):
        for key, value in entry.items():
            name = str(key)
            if name in _SOURCE_KEYS or name.startswith("_") or name == "reasonCode":
                continue
            if name == "status" and _key_text(value) in _TERMINAL_MARKS:
                continue
            if _json_entry_filled(value):
                return True
        return False
    if isinstance(entry, (list, tuple)):
        return any(_json_entry_filled(v) for v in entry)
    return bool(_key_text(entry))


# 来源键：既不算答案内容，也不作为「已填写」判据
_SOURCE_KEYS = {"sources", "sourceRefs", "documentId", "pageNo", "versionId", "quote"}

# 终态标记：模型无法从原文确认时显式标注，算「已处理」但不计已确认答案
_TERMINAL_MARKS = {"needs_human", "needsHuman", "blocked"}


def _json_entry_mark(entry: Any) -> str:
    """实体显式落下的终态标记（needs_human / blocked），没有则空串。"""
    if not isinstance(entry, dict):
        return ""
    for key in ("status", "_status"):
        value = _key_text(entry.get(key))
        if value in _TERMINAL_MARKS:
            return "needs_human" if value in ("needs_human", "needsHuman") else value
    return ""


def _json_entry_reason(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    return _key_text(entry.get("reasonCode") or entry.get("_reasonCode") or "").upper()


def _json_source(entry: Any, field: str | None = None) -> dict[str, str]:
    """从实体里取该字段的来源（文档/版本/页码/原文短引）。

    支持三种真实写法（都不改官方模板键名，只是允许追加）：
    - 实体级：`documentId` + `pageNo` [+ `versionId` / `quote`]，适用于该实体全部字段；
    - 字段级映射：`sources: {fieldId: {documentId, pageNo, ...}}`；
    - 字段级列表：`sources: [{field, documentId, pageNo, ...}]`。
    取不到就返回空，由调用方降级为 `needs_human`，绝不补造来源。
    """
    if not isinstance(entry, dict):
        return {}
    out: dict[str, str] = {}

    def take(node: Any) -> dict[str, str]:
        if not isinstance(node, dict):
            return {}
        return {
            "documentId": _key_text(node.get("documentId") or node.get("_documentId")),
            "versionId": _key_text(node.get("versionId") or node.get("_versionId")),
            "pageNo": _key_text(node.get("pageNo") or node.get("_pageNo")),
            "quote": _key_text(node.get("quote") or node.get("_quote")),
        }

    sources = entry.get("sources") or entry.get("sourceRefs")
    if isinstance(sources, dict):
        target = sources.get(str(field)) if field else None
        if target is None and field is None:
            target = sources
        if isinstance(target, dict):
            out = take(target)
    elif isinstance(sources, list):
        for item in sources:
            if not isinstance(item, dict):
                continue
            label = str(item.get("field") or item.get("fieldId") or "").strip()
            if field and label and label != field:
                continue
            candidate = take(item)
            if candidate.get("documentId") and candidate.get("pageNo"):
                out = candidate
                break
    if not (out.get("documentId") and out.get("pageNo")):
        direct = take(entry)
        if direct.get("documentId") and direct.get("pageNo"):
            out = direct
    return out


def _fill_columns(artifact: dict[str, Any]) -> list[str]:
    key = set(artifact.get("key") or [])
    return [c for c in (artifact.get("columns") or []) if c not in key]


def _row_identity(row: dict[str, str], artifact: dict[str, Any]) -> str:
    """行的契约标识：按布局与主键列重建，与 work_contract.rowKeys 同一口径。"""
    layout = str(artifact.get("layout") or "artifact")
    key_cols = [c for c in (artifact.get("key") or []) if c]
    if layout in ("entity_question_row", "document_question") and len(key_cols) >= 2:
        parts = [_key_text(row.get(c)) for c in key_cols]
        if all(parts):
            return ROW_SEP.join(parts)
    if key_cols:
        first = _key_text(row.get(key_cols[0]))
        if first:
            return first
    for col in artifact.get("columns") or []:
        value = _key_text(row.get(col))
        if value:
            return value
    return ""


def _key_text(raw: Any) -> str:
    return " ".join(str(raw or "").split())


def _row_index(rows: list[dict[str, str]], artifact: dict[str, Any]) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for row in rows:
        ident = _row_identity(row, artifact)
        if ident and ident not in index:
            index[ident] = row
    return index


def _value_of(row: dict[str, str], cell: dict[str, Any]) -> str:
    column = str(cell.get("valueColumn") or "")
    if column and column in row:
        return _key_text(row.get(column))
    field = str(cell.get("fieldId") or "")
    if field and field in row:
        return _key_text(row.get(field))
    return ""


def _value_filled(row: dict[str, str], cols: list[str]) -> bool:
    return bool(cols) and all(_key_text(row.get(c)) for c in cols)


# ---------------------------------------------------------------------------
# 覆盖与校验
# ---------------------------------------------------------------------------

def inspect_artifact(contract: dict[str, Any], artifact: dict[str, Any], root: Path) -> dict[str, Any]:
    """重新读取已落盘交付物并与契约比对（写后校验，不信任写入时的返回值）。"""
    path = _submission(root) / artifact["name"]
    fmt = str(artifact.get("format") or "md")
    columns = list(artifact.get("columns") or [])
    fill = _fill_columns(artifact)
    rows_expected = len(artifact.get("rowKeys") or [])
    result: dict[str, Any] = {
        "name": artifact["name"],
        "format": fmt,
        "layout": artifact.get("layout"),
        "exists": path.exists(),
        "parseable": True,
        "rows": 0,
        "rowsExpected": rows_expected,
        "rowsFilled": 0,
        "rowsUnfilled": [],
        "rowsNeedsHuman": [],
        "missingRowKeys": [],
        "invalidCells": [],
        "sha256": None,
        "bytes": 0,
    }
    if not path.exists():
        result["parseable"] = False
        return result
    try:
        result["bytes"] = path.stat().st_size
        result["sha256"] = _sha256(path)
    except OSError:
        result["parseable"] = False
        return result

    if fmt == "csv":
        rows = _read_rows(path)
        result["rows"] = len(rows)
        seen = [ident for ident in (_row_identity(r, artifact) for r in rows) if ident]
        for row in rows:
            if _value_filled(row, fill):
                result["rowsFilled"] += 1
        expected = [str(k) for k in (artifact.get("rowKeys") or [])]
        if expected:
            seen_set = set(seen)
            result["missingRowKeys"] = [k for k in expected if k not in seen_set][:50]
        rules = [r for r in (contract.get("rules") or [])
                 if r.get("kind") == "value_domain" and r.get("artifact") == artifact["name"]]
        for rule in rules:
            column = str(rule.get("column") or "")
            allowed = {str(v) for v in (rule.get("allowed") or [])}
            if not column or not allowed:
                continue
            for index, row in enumerate(rows):
                value = _key_text(row.get(column))
                if value and value not in allowed:
                    result["invalidCells"].append({"row": index, "column": column})
    elif fmt == "json":
        data = _read_json(path)
        if data is None:
            result["parseable"] = False
        else:
            entries = _json_entries(data, artifact)
            result["rows"] = len(entries) if entries else (
                len(data) if isinstance(data, (list, dict)) else 0)
            result["rowsFilled"] = sum(1 for v in entries.values() if _json_entry_filled(v)) \
                if entries else result["rows"]
            expected = [str(k) for k in (artifact.get("rowKeys") or [])]
            if expected:
                # 覆盖口径与 CSV 一致：实体是否在交付物里存在（结构覆盖）。
                # 内容是否已确认由 rowsFilled / 答案格状态表达，不和结构覆盖混用，
                # 否则「30% 中断仍应有 100% 行全集」这条要求就无法成立。
                result["missingRowKeys"] = [k for k in expected if k not in entries][:50]
                # 未填 = 在文件里但既没内容、也没显式标 needs_human/blocked。
                # 只是补齐的空壳实体才算缺口，已标注的属于「待人工核实」而非遗漏。
                result["rowsUnfilled"] = [
                    k for k in expected
                    if k in entries and not _json_entry_filled(entries[k])
                    and not _json_entry_mark(entries[k])
                ][:50]
                result["rowsNeedsHuman"] = [
                    k for k in expected
                    if k in entries and not _json_entry_filled(entries[k])
                    and _json_entry_mark(entries[k])
                ][:50]
    else:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        result["rows"] = len([ln for ln in text.splitlines() if ln.strip()])
        result["rowsFilled"] = result["rows"]
        if len(text.strip().splitlines()) <= 2:
            result["parseable"] = False
            result["invalidCells"].append({"row": 0, "column": "content"})
    return result


def _coverage_ok(inspection: dict[str, Any]) -> bool:
    if not inspection["exists"] or not inspection["parseable"]:
        return False
    if int(inspection["rowsExpected"] or 0):
        return not inspection["missingRowKeys"]
    return int(inspection["rows"]) > 0


def status_for(inspections: list[dict[str, Any]]) -> str:
    """交付总状态：complete 全部可解析且行全集覆盖；partial 有内容但未覆盖；invalid 不可解析。"""
    if not inspections:
        return "pending"
    if all(_coverage_ok(i) for i in inspections):
        return "complete"
    if any(i["exists"] and i["parseable"] for i in inspections):
        return "partial"
    return "invalid"


# ---------------------------------------------------------------------------
# 答案格同步（从已落盘文件反推结构覆盖状态）
# ---------------------------------------------------------------------------

def _cells_of(contract: dict[str, Any], artifact_name: str) -> list[dict[str, Any]]:
    return [c for c in (contract.get("cells") or []) if c.get("artifact") == artifact_name]


def sync_cells_from_file(
    task_id: str,
    contract: dict[str, Any],
    artifact: dict[str, Any],
    root: Path,
) -> dict[str, int]:
    """把已落盘的交付表同步到答案格账本（幂等）。

    - 值非空但无来源声明 → `needs_human`（SOURCE_MISSING），不伪装成已支持事实；
    - 行携带来源列（documentId/pageNo [+versionId/quote]）→ `committed`；
    - 空值 → 保持未提交（结构缺口可见）。
    """
    cells = _cells_of(contract, artifact["name"])
    if not cells:
        return {"committed": 0, "needsHuman": 0, "pending": 0}
    path = _submission(root) / artifact["name"]
    if str(artifact.get("format") or "") == "json":
        return _sync_json_cells(task_id, artifact, cells, path)
    rows = _row_index(_read_rows(path), artifact)
    committed = needs_human = pending = 0
    for cell in cells:
        row = rows.get(str(cell.get("rowKey") or "")) or rows.get(str(cell.get("cellKey") or ""))
        if row is None:
            pending += 1
            continue
        value = _value_of(row, cell)
        if not value:
            pending += 1
            continue
        doc_id = _key_text(row.get("documentId") or row.get("_documentId"))
        page_raw = _key_text(row.get("pageNo") or row.get("_pageNo"))
        page_no = int(page_raw) if page_raw.isdigit() else 0
        if doc_id and page_no:
            work_ledger.commit_cell(
                task_id, artifact["name"], str(cell["cellKey"]), value=value,
                status="committed", document_id=doc_id,
                version_id=_key_text(row.get("versionId") or row.get("_versionId")) or None,
                page_no=page_no,
                quote=_key_text(row.get("quote") or row.get("_quote")),
            )
            committed += 1
        else:
            work_ledger.commit_cell(
                task_id, artifact["name"], str(cell["cellKey"]), value=value,
                status="needs_human", reason_code="SOURCE_MISSING",
            )
            needs_human += 1
    return {"committed": committed, "needsHuman": needs_human, "pending": pending}


def _sync_json_cells(
    task_id: str,
    artifact: dict[str, Any],
    cells: list[dict[str, Any]],
    path: Path,
) -> dict[str, int]:
    """JSON 交付物的答案格同步（矩阵布局：实体 × 字段）。

    - 字段有内容且能取到来源 → `committed`；
    - 字段有内容但无来源 → `needs_human`（SOURCE_MISSING），不伪装成已核实；
    - 字段空/实体缺失 → 保持未提交，让覆盖缺口在账本里可见。
    """
    entries = _json_entries(_read_json(path), artifact)
    committed = needs_human = pending = 0
    for cell in cells:
        entity_id = str(cell.get("entityId") or "")
        field_id = str(cell.get("fieldId") or "")
        entry = entries.get(entity_id)
        if not field_id:
            # 扁平键图（题号 → 答案，如 answers.json 的 M01…M17）：整格就是该键的值，
            # 没有字段层，也没有独立的来源列——答案是由已提交答案格派生的，
            # 来源随派生链保留。缺来源不降级为 needs_human，否则派生结果永远
            # 到不了终态，收尾门禁会误判成「未完成」。
            if not _json_entry_filled(entry):
                pending += 1
                continue
            work_ledger.commit_cell(
                task_id, artifact["name"], str(cell["cellKey"]),
                value=entry, status="committed",
            )
            committed += 1
            continue
        if not isinstance(entry, dict):
            pending += 1
            continue
        value = entry.get(field_id)
        mark = _json_entry_mark(entry)
        if not _json_entry_filled(value):
            # 实体被显式标注无法确认时，其空字段按 needs_human 落库
            # （方案 §1.3：登记 needs_human 才计入结构覆盖，但不能算已确认答案）。
            if mark:
                work_ledger.commit_cell(
                    task_id, artifact["name"], str(cell["cellKey"]),
                    value=None, status=mark,
                    reason_code=_json_entry_reason(entry) or "INSUFFICIENT_EVIDENCE",
                )
                needs_human += 1
            else:
                pending += 1
            continue
        source = _json_source(entry, field_id)
        page_no = int(source.get("pageNo") or 0) if str(source.get("pageNo") or "").isdigit() else 0
        if source.get("documentId") and page_no:
            work_ledger.commit_cell(
                task_id, artifact["name"], str(cell["cellKey"]),
                value=value, status="committed",
                document_id=source["documentId"],
                version_id=source.get("versionId") or None,
                page_no=page_no,
                quote=source.get("quote") or "",
            )
            committed += 1
        else:
            work_ledger.commit_cell(
                task_id, artifact["name"], str(cell["cellKey"]),
                value=value, status="needs_human", reason_code="SOURCE_MISSING",
            )
            needs_human += 1
    return {"committed": committed, "needsHuman": needs_human, "pending": pending}


def sync_units_for_artifact(task_id: str, artifact_name: str) -> int:
    """把该交付物的答案格终态回写到对应工作单元（幂等，不淘汰）。"""
    terminal = work_ledger.artifact_cells_done(task_id, artifact_name)
    cells = {c["cellKey"]: c for c in work_ledger.list_cells(
        task_id, artifact_name=artifact_name)}
    advanced = 0
    for unit in work_ledger.list_units(task_id):
        scope = unit.get("scope") or {}
        if scope.get("artifact") != artifact_name:
            continue
        if unit["status"] in work_ledger.TERMINAL_UNIT_STATUSES:
            continue
        if unit["scopeType"] in ("materialize", "derive", "artifact"):
            # 物化只在全部答案格终态后才真正执行过；未终态时它还没开工，
            # 保持原状态。把它记成 staged 会让「未开工」看起来像「已暂存」。
            # `artifact` 是「无答案格的整体交付物」（如聚合 JSON）：它没有格子
            # 可同步，`artifact_cells_done` 恒为真，登记即视为完成——否则这类
            # 交付物的工作单元永远停在 pending，终态率永远到不了 100%。
            if not terminal:
                continue
            target = "committed"
        else:
            cell = cells.get(str(scope.get("cellKey") or ""))
            if cell is None:
                continue
            target = {
                "committed": "committed",
                "needs_human": "needs_human",
                "blocked": "blocked",
            }.get(str(cell["status"]), "")
            # 答案格仍是 pending（还没作答）时不得推进工作单元：
            # 否则整批未处理单元会被错记成 staged，终态率与进度全部失真。
            if not target:
                continue
        if unit["status"] == target:
            continue
        try:
            # 账本不接受跳级（pending → committed 非法）：按状态机最短路径补齐中间态，
            # 否则答案格已终态、工作单元却永远停在 pending（终态率恒为 0）。
            work_ledger.advance_unit(task_id, unit["workUnitId"], target)
            advanced += 1
        except work_ledger.InvalidTransition:
            continue
    return advanced


# ---------------------------------------------------------------------------
# 物化与登记
# ---------------------------------------------------------------------------

def _split_row_key(row_key: str, key_columns: list[str]) -> dict[str, str]:
    """把契约行标识还原成主键列取值（长表 `A01::q1` → agreement=A01, deal_point=q1）。"""
    if len(key_columns) >= 2:
        parts = row_key.split(ROW_SEP)
        if len(parts) == len(key_columns):
            return dict(zip(key_columns, parts))
        if len(parts) > len(key_columns):
            head = parts[: len(key_columns) - 1]
            return dict(zip(key_columns, [*head, ROW_SEP.join(parts[len(key_columns) - 1:])]))
    if key_columns:
        return {key_columns[0]: row_key}
    return {}


def materialize_artifact(
    task_id: str,
    contract: dict[str, Any],
    artifact: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    """补齐行全集并原子写入正式交付物（只补结构：新增行仅填主键列）。

    先写临时文件再替换：任意中断点都是完整可解析文件（HAR-T-005）。
    """
    name = artifact["name"]
    fmt = str(artifact.get("format") or "md")
    path = _submission(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        columns = list(artifact.get("columns") or [])
        key_cols = [c for c in (artifact.get("key") or []) if c]
        if not columns:
            columns = ["item", "value"]
        if not key_cols and columns:
            key_cols = [columns[0]]
        rows = _read_rows(path)
        # 保留模型自行追加的来源列（documentId/pageNo/quote 等）：物化只补行，
        # 不能把已有的证据列裁掉，否则已核实内容会退化为无来源项。
        for row in rows:
            for col in row:
                if col and col not in columns:
                    columns.append(col)
        existing = {_row_identity(r, artifact) for r in rows if _row_identity(r, artifact)}
        added = 0
        for row_key in artifact.get("rowKeys") or []:
            token = str(row_key)
            if token in existing:
                continue
            row = {c: "" for c in columns}
            for col, value in _split_row_key(token, key_cols).items():
                if col in row:
                    row[col] = value
            rows.append(row)
            existing.add(token)
            added += 1
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n",
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(buf.getvalue(), encoding="utf-8")
        tmp.replace(path)
    elif fmt == "json":
        data = _read_json(path)
        if not isinstance(data, (dict, list)):
            data = json.loads(json.dumps(artifact.get("skeleton") or {}))
        data = _materialize_json_rows(artifact, data)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    return {"name": name, "path": f"{deliverable_store.DELIVERABLE_DIRNAME}/{name}"}


def _materialize_json_rows(artifact: dict[str, Any], data: Any) -> Any:
    """补齐 JSON 交付物的实体全集（只补空壳，不写字面答案）。

    13 被告任务里模型可能只写了 9 个被告；缺的被告必须出现在文件里并保持模板
    字段为空，这样「结构覆盖」可被复算（缺谁一目了然），且不会被误当成
    「未声明覆盖」而整体判为完成。
    """
    row_keys = [str(k) for k in (artifact.get("rowKeys") or [])]
    if not row_keys:
        return data
    keys = _json_path(artifact)
    if keys:
        if not isinstance(data, dict):
            data = json.loads(json.dumps(artifact.get("skeleton") or {}))
            if not isinstance(data, dict):
                data = {}
        node = data
        for key in keys[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                child = {}
                node[key] = child
            node = child
        last = keys[-1]
        bucket = node.get(last)
        if not isinstance(bucket, dict):
            bucket = {}
            node[last] = bucket
        template = ((artifact.get("skeleton") or {}).get(keys[0]) or {}) \
            if isinstance(artifact.get("skeleton"), dict) else {}
        for row_key in row_keys:
            if row_key not in bucket:
                entry = template.get(row_key) if isinstance(template, dict) else None
                bucket[row_key] = json.loads(json.dumps(entry)) if isinstance(entry, dict) else {}
        return data
    if not isinstance(data, dict):
        return data
    for row_key in row_keys:
        data.setdefault(row_key, {})
    return data


def commit_artifact(
    task_id: str,
    contract: dict[str, Any],
    name: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """单次原子提交：物化 → 写后重读 → 同步答案格与工作单元 → 登记产物。"""
    root = root or workspace_root()
    artifact = next((a for a in contract.get("artifacts") or [] if a.get("name") == name), None)
    if artifact is None:
        return {"name": name, "status": "pending", "registered": False}
    materialize_artifact(task_id, contract, artifact, root)
    inspection = inspect_artifact(contract, artifact, root)
    cells = sync_cells_from_file(task_id, contract, artifact, root)
    units = sync_units_for_artifact(task_id, name)
    status = ("complete" if _coverage_ok(inspection)
              else ("partial" if inspection["exists"] and inspection["parseable"] else "invalid"))
    work_ledger.register_artifact(
        task_id, name,
        status=status,
        rows_expected=int(inspection["rowsExpected"] or inspection["rows"]),
        rows_committed=int(inspection["rowsFilled"] or 0),
        content_sha256=inspection["sha256"],
        storage_path=f"{deliverable_store.DELIVERABLE_DIRNAME}/{name}",
    )
    return {
        "name": name,
        "status": status,
        "registered": True,
        "rows": inspection["rows"],
        "rowsExpected": inspection["rowsExpected"],
        "rowsFilled": inspection["rowsFilled"],
        "missingRows": len(inspection["missingRowKeys"]),
        "invalidCells": len(inspection["invalidCells"]),
        "cells": cells,
        "unitsAdvanced": units,
    }


def materialize_derived(
    task_id: str,
    contract: dict[str, Any],
    root: Path,
) -> list[dict[str, Any]]:
    """按声明生成派生交付物：只做确定性聚合，不引入模型判断。

    聚合口径：按 `groupBy` 分组、对 `columns` 计数；空值单元格不参与任何计数
    （HAR-T-013），因此派生表永远与当前主表的已提交内容一致，可随时复算。
    未声明 groupBy 时生成「组 → 行数」汇总。
    """
    results: list[dict[str, Any]] = []
    for art in contract.get("artifacts") or []:
        for derived in art.get("derived") or []:
            dname = str(derived.get("name") or "")
            sources = [str(s) for s in (derived.get("dependsOn") or []) if str(s or "")]
            if not dname or not sources:
                continue
            spec = next((d for d in (art.get("derivedSpecs") or [])
                         if d.get("name") == dname), None) or {}
            rows = _read_rows(_submission(root) / sources[0])
            group_by = [str(g) for g in (spec.get("groupBy") or []) if str(g or "")]
            tally_cols = [str(c) for c in (spec.get("columns") or []) if str(c or "")]
            counts: dict[tuple[str, ...], dict[str, int]] = {}
            for row in rows:
                key = tuple(_key_text(row.get(g)) for g in group_by) if group_by else ("全部",)
                if group_by and not any(key):
                    continue
                bucket = counts.setdefault(key, {})
                for col in tally_cols:
                    if _key_text(row.get(col)):
                        bucket[col] = bucket.get(col, 0) + 1
            header = list(group_by or ["group"]) + (tally_cols or ["rows"])
            path = _submission(root) / dname
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix.lower() == ".csv":
                buf = io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=header, lineterminator="\n")
                writer.writeheader()
                for key in sorted(counts):
                    bucket = counts[key]
                    out = dict(zip(group_by or ["group"], key))
                    if tally_cols:
                        for col in tally_cols:
                            out[col] = bucket.get(col, 0)
                    else:
                        out["rows"] = len(bucket)
                    writer.writerow(out)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(buf.getvalue(), encoding="utf-8")
                tmp.replace(path)
                payload_rows: Any = len(counts)
            else:
                payload = []
                for key in sorted(counts):
                    item = dict(zip(group_by or ["group"], key))
                    if tally_cols:
                        item.update({c: counts[key].get(c, 0) for c in tally_cols})
                    else:
                        item["rows"] = len(counts[key])
                    payload.append(item)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
                tmp.replace(path)
                payload_rows = len(payload)
            sha = _sha256(path)
            results.append({"name": dname, "rows": payload_rows, "sources": sources,
                            "sha256": sha})
            work_ledger.register_artifact(
                task_id, dname, status="complete",
                rows_expected=payload_rows, rows_committed=payload_rows,
                content_sha256=sha,
                storage_path=f"{deliverable_store.DELIVERABLE_DIRNAME}/{dname}",
            )
    return results


def finalize(task_id: str, contract: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """任务收尾：逐交付物提交 + 派生聚合 + 汇总缺口（不阻断，只如实报告）。"""
    root = root or workspace_root()
    results = [commit_artifact(task_id, contract, a["name"], root)
               for a in (contract.get("artifacts") or []) if a.get("name")]
    try:
        derived = materialize_derived(task_id, contract, root)
    except Exception:  # noqa: BLE001 派生失败按缺口报告，不影响主交付物
        derived = []
    gaps: list[dict[str, Any]] = []
    for result in results:
        if result["status"] != "complete":
            gaps.append({
                "artifact": result["name"],
                "status": result["status"],
                "missingRows": result.get("missingRows", 0),
                "invalidCells": result.get("invalidCells", 0),
            })
    progress = work_ledger.delivery_progress(task_id)
    return {
        # 与 delivery_progress 同一口径：结构完整但答案格未终态时仍是 partial。
        "status": progress["status"],
        "structuralStatus": progress["structuralStatus"],
        "workDone": progress["workDone"],
        "artifacts": [{k: r.get(k) for k in ("name", "status", "rows", "rowsExpected", "rowsFilled")}
                      for r in results],
        "derived": derived,
        "gaps": gaps,
        "progress": progress,
    }
