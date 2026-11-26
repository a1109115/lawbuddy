"""交付物骨架与周期回写（WFA-LAW 优化方案 §4.2 / P0-04）。

背景：长任务（大卷宗、多文档聚合）此前把"先落盘再完善"只写在提示词里，
属于软约束。实测在轮数/工具调用预算耗尽时 `submission/` 仍为空——模型把
108 次写文件全部用于临时探测脚本，从未写交付表格，官方评分器直接 0 分。

本模块把交付从"模型自觉"变成"结构约束"：
- 任务启动即为声明的交付物生成可解析骨架（CSV 表头 / JSON 模板 / MD 标题）；
- 提供 `deliverable_patch` 做按主键的 upsert（避免整文件重写与丢内容）；
- 交付契约可由任务创建时显式声明，也可从请求文本里点名的输出文件名推导。

骨架文件只是结构占位，不含任何推断事实——内容仍由模型经原文查证后回写，
因此不影响"结论必须基于原文"的底线。
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

DELIVERABLE_DIRNAME = "submission"   # 交付物落盘子目录（相对工作空间根）
CSV_MAX_ROWS = 5000                  # 单文件行数上限，防止误把整卷正文写进交付表

# 已知交付契约（WorkflowArena LAW 提交格式）。仅在任务未显式声明 deliverables、
# 但请求文本点名了这些输出文件时用于补齐表头；显式声明始终优先。
_KNOWN_SCHEMAS: dict[str, dict[str, Any]] = {
    "deal_points.csv": {
        "format": "csv",
        "columns": ["agreement", "deal_point", "answer"],
        "key": ["agreement", "deal_point"],
    },
    "answers.json": {"format": "json", "skeleton": {}},
    "judgment.json": {"format": "json", "skeleton": {}},
}

# 请求文本中出现的交付文件名（带已知扩展名），如 deal_points.csv / judgment.json
_REQUEST_FILE_RE = re.compile(r"[\w\u4e00-\u9fa5][\w\u4e00-\u9fa5.\-]*\.(?:csv|json|md|txt)\b")
_SUPPORTED_FORMATS = ("csv", "json", "md")


def _clean_name(raw: Any) -> str:
    """交付文件名规范化：去掉目录成分，只保留 submission 下的一层文件名。"""
    name = str(raw or "").strip().strip('"\'`')
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(". ")
    return name[:120]


def _format_of(name: str) -> str:
    suffix = Path(name).suffix.lower().lstrip(".")
    return suffix if suffix in _SUPPORTED_FORMATS else "md"


def _as_text_list(raw: Any) -> list[str]:
    """字符串数组规范化：去空、去重、保序（不推断任何业务含义）。"""
    if isinstance(raw, dict):
        raw = [k for k, v in raw.items() if v]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _normalize_entities(raw: Any) -> list[dict[str, Any]]:
    """expectedEntities 规范化（B-01）：接受字符串或对象，保留 id/label/fields。"""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            label = str(item.get("label") or item.get("name") or item.get("id") or "").strip()
            entity = {
                "id": str(item.get("id") or label).strip(),
                "label": label or str(item.get("id") or "").strip(),
                "fields": _as_text_list(item.get("fields")),
            }
        else:
            label = str(item or "").strip()
            entity = {"id": label, "label": label, "fields": []}
        if not entity["id"] or entity["id"] in seen:
            continue
        seen.add(entity["id"])
        out.append(entity)
    return out


def _normalize_questions(raw: Any) -> list[dict[str, Any]]:
    """questions 规范化（B-01）：接受字符串或对象，保留 id/text。"""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("question") or "").strip()
            qid = str(item.get("id") or "").strip() or f"q{index}"
        else:
            text = str(item or "").strip()
            qid = f"q{index}"
        if not text or qid in seen:
            continue
        seen.add(qid)
        out.append({"id": qid, "text": text})
    return out


def _normalize_derived(raw: Any) -> list[dict[str, Any]]:
    """派生交付物声明（B-01）：名称 + 依赖的主交付物 + 可选聚合规则。

    只支持确定性聚合（按声明列分组计数）：派生文件必须能由当前主表复算，
    不允许模型自定义聚合口径，也不允许把未确认单元格按否定值统计。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        spec = item if isinstance(item, dict) else {}
        name = _clean_name(spec.get("name") or spec.get("fileName"))
        if not name or any(d["name"] == name for d in out):
            continue
        out.append({
            "name": name,
            "dependsOn": _as_text_list(spec.get("dependsOn") or spec.get("sources")),
            "description": str(spec.get("description") or "").strip(),
            "groupBy": _as_text_list(spec.get("groupBy")),
            "columns": _as_text_list(spec.get("columns")),
        })
    return out


def _normalize_one(raw: Any) -> dict[str, Any] | None:
    spec = raw if isinstance(raw, dict) else {}
    name = _clean_name(spec.get("name") or spec.get("fileName") or spec.get("path"))
    if not name:
        return None
    known = _KNOWN_SCHEMAS.get(name.lower(), {})
    fmt = str(spec.get("format") or known.get("format") or _format_of(name)).lower()
    if fmt not in _SUPPORTED_FORMATS:
        fmt = _format_of(name)
    out: dict[str, Any] = {
        "name": name,
        "format": fmt,
        "title": str(spec.get("title") or "").strip(),
        "description": str(spec.get("description") or "").strip(),
    }
    if fmt == "csv":
        columns = spec.get("columns") or known.get("columns") or []
        out["columns"] = [str(c).strip() for c in columns if str(c or "").strip()]
        key = spec.get("key") or known.get("key") or []
        out["key"] = [str(k).strip() for k in key if str(k or "").strip()]
        # 多选字段（P2-09）：声明哪些列是多选，提示词据此要求「逐项核对后以 ` | ` 连接」，
        # 也让 _needs_option_check 能在任务启动时就识别出需要逐项核对。
        multi_points = spec.get("multiPoints") or spec.get("multi") or []
        if isinstance(multi_points, dict):
            multi_points = [k for k, v in multi_points.items() if v]
        if isinstance(multi_points, list):
            out["multiPoints"] = [str(p).strip() for p in multi_points if str(p or "").strip()]
    elif fmt == "json":
        skeleton = spec.get("skeleton")
        out["skeleton"] = skeleton if isinstance(skeleton, (dict, list)) else known.get("skeleton", {})
        # 数组路径：`path`（如 ["defendants"]）与 `jsonPath`（如 "defendants"）同义。
        # 声明后交付控制器会把它当作「键集合必须齐全的对象」来校验覆盖，
        # 而不是只判断顶层文件非空（13 名被告缺一个也算未覆盖）。
        raw_path = spec.get("path", spec.get("jsonPath"))
        if isinstance(raw_path, str) and raw_path.strip():
            out["path"] = [p for p in raw_path.split(".") if p.strip()]
        elif isinstance(raw_path, list):
            out["path"] = [str(p) for p in raw_path if str(p or "").strip()]
        item_key = str(spec.get("itemKey") or "").strip()
        if item_key:
            out["itemKey"] = item_key
    # 工作全集相关声明（B-01）：实体、问题、合法值、派生依赖。
    # 这些字段只描述「必须覆盖什么」，不含任何事实答案；规范化后必须原样保留，
    # 否则契约编译器无法生成 300 格 / 13 被告这类确定的覆盖结构。
    entities = _normalize_entities(spec.get("expectedEntities") or spec.get("entities"))
    if entities:
        out["expectedEntities"] = entities
    questions = _normalize_questions(spec.get("questions") or spec.get("items"))
    if questions:
        out["questions"] = questions
    valid_values = spec.get("validValues") or spec.get("allowedValues")
    if isinstance(valid_values, dict):
        cleaned_values = {
            str(k): _as_text_list(v) for k, v in valid_values.items() if _as_text_list(v)
        }
        if cleaned_values:
            out["validValues"] = cleaned_values
    # 合法值的作用位置（B-01）：optionKey 指明哪一列携带"问题标识"，
    # valueColumn 指明答案列；缺了它们，写入校验无法把行映射回该题的选项集。
    option_key = str(spec.get("optionKey") or "").strip()
    if option_key:
        out["optionKey"] = option_key
    value_column = str(spec.get("valueColumn") or "").strip()
    if value_column:
        out["valueColumn"] = value_column
    derived = _normalize_derived(spec.get("derivedArtifacts"))
    if derived:
        out["derivedArtifacts"] = derived
    return out


def normalize_specs(raw: Any) -> list[dict[str, Any]]:
    """把任务携带的 deliverables 规范化为内部契约（无效项丢弃，同名只保留首个）。"""
    if not isinstance(raw, list):
        return []
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        spec = _normalize_one(item)
        if spec is None or spec["name"] in seen:
            continue
        seen.add(spec["name"])
        specs.append(spec)
    return specs


def derive_from_request(request: str, limit: int = 4) -> list[dict[str, Any]]:
    """从请求文本里点名的输出文件名推导交付契约（任务未显式声明时的兜底）。

    只收带数据/文档扩展名的显式文件名，避免把"分析报告"这类泛称当成文件；
    顺序按首次出现，最多 limit 个，防止请求里随口提到的临时文件名也建骨架。
    """
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _REQUEST_FILE_RE.finditer(str(request or "")):
        name = _clean_name(m.group(0))
        if not name or name.lower() in seen:
            continue
        # 请求文本里的脚本/数据临时文件不是交付物
        if name.lower().endswith((".py", ".sh", ".log")):
            continue
        seen.add(name.lower())
        spec = _normalize_one({"name": name})
        if spec is not None:
            specs.append(spec)
        if len(specs) >= limit:
            break
    return specs


def submission_dir(root: Path) -> Path:
    return root / DELIVERABLE_DIRNAME


def _skeleton_content(spec: dict[str, Any]) -> str:
    fmt = spec["format"]
    if fmt == "csv":
        columns = spec.get("columns") or ["item", "value"]
        buf = io.StringIO()
        csv.DictWriter(buf, fieldnames=columns, lineterminator="\n").writeheader()
        return buf.getvalue()
    if fmt == "json":
        skeleton = spec.get("skeleton")
        return json.dumps(skeleton if skeleton is not None else {}, ensure_ascii=False, indent=2) + "\n"
    title = spec.get("title") or Path(spec["name"]).stem
    return f"# {title}\n\n"


def ensure_skeleton(root: Path, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """生成缺失的交付骨架，返回每个交付物的当前状态（已存在不覆盖，防抹掉已回写内容）。"""
    if not specs:
        return []
    directory = submission_dir(root)
    states: list[dict[str, Any]] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [{"name": s["name"], "created": False, "error": str(exc)} for s in specs]
    for spec in specs:
        path = directory / spec["name"]
        created = False
        if not path.exists():
            try:
                path.write_text(_skeleton_content(spec), encoding="utf-8")
                created = True
            except OSError:
                states.append({"name": spec["name"], "created": False, "error": "写入骨架失败"})
                continue
        states.append({
            "name": spec["name"],
            "path": f"{DELIVERABLE_DIRNAME}/{spec['name']}",
            "format": spec["format"],
            "created": created,
            "bytes": path.stat().st_size if path.exists() else 0,
        })
    return states


def describe(specs: list[dict[str, Any]]) -> str:
    """交付契约的提示词段：让模型知道骨架已就位、用哪个工具回写。"""
    if not specs:
        return ""
    lines = [
        "",
        f"【交付契约】本次任务的交付物落盘目录：{DELIVERABLE_DIRNAME}/（骨架已生成，可直接回写）",
    ]
    for spec in specs:
        hint = ""
        if spec["format"] == "csv":
            cols = "、".join(spec.get("columns") or [])
            key = "、".join(spec.get("key") or [])
            hint = f"（CSV 列：{cols}" + (f"；主键：{key}" if key else "") + "）"
            multi = spec.get("multiPoints") or []
            valid = spec.get("validValues") or {}
            if valid:
                # 只报题量与"逐字"要求：选项原文已在任务材料里，逐条铺开会让
                # 提示词膨胀；写错时由写入校验把该题可用选项回传。
                hint += f"；{len(valid)} 个字段有标准选项，answer 必须逐字等于其中一项"
            if multi:
                hint += (
                    "；多选列：" + "、".join(multi) + "（每个选项都要逐项回查原文确认，"
                    "选中的写成以 ` | ` 连接的完整选项文本，不要只写部分项）"
                )
        elif spec["format"] == "json":
            if spec.get("path") and spec.get("itemKey"):
                hint = f"（JSON 数组：{'/'.join(spec['path'])}；元素主键：{spec['itemKey']}）"
            elif spec.get("questions"):
                ids = [str(q.get("id") or "") for q in spec["questions"] if q.get("id")]
                shown = "、".join(ids[:6]) + ("…" if len(ids) > 6 else "")
                hint = (f"（JSON 对象，必须且只能使用这些键：{shown}"
                        f"，共 {len(ids)} 个；缺键即视为该题未答）")
            else:
                hint = "（JSON 对象，按字段合并）"
        desc = f"：{spec['description']}" if spec.get("description") else ""
        lines.append(f"- {DELIVERABLE_DIRNAME}/{spec['name']}{hint}{desc}")
    lines.append(
        "回写规则：每确认一条内容就用 deliverable_patch 立即写入（按主键 upsert，重复写同一主键"
        "只更新该行/该节点，不会丢已有内容）；不要等全部分析完成才一次性写文件，"
        "也不要用 write_file 整文件覆盖已回写的交付物。"
        "交付物中只写经原文核实的内容；不确定项留空或在备注列标注待核实，不得编造。"
    )
    return "\n".join(lines)


def _read_csv(path: Path, columns: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace") if path.exists() else ""
    if not text.strip():
        return [], list(columns)
    reader = csv.DictReader(io.StringIO(text))
    rows = [{(k or "").strip(): (v if isinstance(v, str) else "") for k, v in r.items()} for r in reader]
    header = [h for h in (reader.fieldnames or []) if h]
    merged = list(columns)
    for h in header:
        if h not in merged:
            merged.append(h)
    return rows, merged


def _write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    path.write_text(buf.getvalue(), encoding="utf-8")


def _option_violations(spec: dict[str, Any], rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """按声明合法值校验待写入的行（B-01：合法值是契约的一部分）。

    `validValues` 的键是"问题标识"，`optionKey` 指明哪一列携带该标识，
    `valueColumn` 指明答案列；多选项按 ` | ` 拆分后逐个比对。
    实测模型会把自己的措辞当答案写回（"Natural disaster" → "natural disasters"、
    "No" → "None of the above"），官方按逐字匹配判分，整列归零；这里在落盘前
    拒绝并回传该题的标准选项，让模型可立即改正，而不是把错值写成确定事实。
    """
    valid = spec.get("validValues") or {}
    option_key = str(spec.get("optionKey") or "")
    value_column = str(spec.get("valueColumn") or "")
    if not valid or not option_key or not value_column:
        return []
    multi = {str(m) for m in (spec.get("multiPoints") or [])}
    out: list[dict[str, Any]] = []
    for row in rows:
        qid = str(row.get(option_key) or "").strip()
        options = valid.get(qid)
        if not options:
            continue
        value = str(row.get(value_column) or "").strip()
        if not value:
            continue
        parts = [p.strip() for p in value.split("|")] if qid in multi else [value]
        invalid = [p for p in parts if p and p not in options]
        if invalid:
            out.append({
                "row": {k: row.get(k, "") for k in (spec.get("key") or [option_key])},
                "value": value, "invalid": invalid, "allowed": list(options),
            })
    return out


def _patch_csv(path: Path, spec: dict[str, Any], rows: list[Any]) -> dict[str, Any]:
    columns = list(spec.get("columns") or [])
    key = list(spec.get("key") or [])
    existing, columns = _read_csv(path, columns)
    if not columns:
        raise ValueError("该 CSV 交付物未声明列（columns），请先用 write_file 写表头或补充声明")
    upserted = appended = 0
    prepared = [{str(k): ("" if v is None else str(v)) for k, v in raw.items()}
                for raw in rows if isinstance(raw, dict)]
    violations = _option_violations(spec, prepared)
    if violations:
        sample = violations[:3]
        detail = "；".join(
            f"{v['row']} 的答案 {v['invalid']} 不在标准选项内（可用：{' / '.join(v['allowed'])}）"
            for v in sample
        )
        raise ValueError(
            f"answer 必须逐字使用该 deal point 的 standard option，本次写入已拒绝（{len(violations)} 行不合规）：{detail}。"
            "请按上列标准选项原文重写，多选用 ` | ` 连接；确无匹配选项时留空并在 quote 注明待核实。"
        )
    for row in prepared:
        for col in row:
            if col not in columns:
                columns.append(col)
        target = None
        if key and all(str(row.get(k, "")).strip() for k in key):
            target = next(
                (r for r in existing if all(str(r.get(k, "")).strip() == str(row[k]).strip() for k in key)),
                None,
            )
        if target is None:
            existing.append(row)
            appended += 1
        else:
            target.update(row)
            upserted += 1
    if len(existing) > CSV_MAX_ROWS:
        raise ValueError(f"交付表行数超上限（{CSV_MAX_ROWS}），请检查是否误写入非交付内容")
    _write_csv(path, existing, columns)
    return {"written": True, "rows": len(existing), "upserted": upserted, "appended": appended}


def _deep_merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = _deep_merge(out.get(k), v)
        return out
    if isinstance(base, list) and isinstance(patch, list):
        return base + patch
    return patch


def _patch_json(path: Path, spec: dict[str, Any], rows: list[Any], *, merge: Any = None) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        data = json.loads(json.dumps(spec.get("skeleton") or {}))
    if not isinstance(data, (dict, list)):
        data = {}
    path_keys = list(spec.get("path") or [])
    item_key = str(spec.get("itemKey") or "")
    upserted = appended = 0
    if path_keys and item_key:
        if not isinstance(data, dict):
            data = {}
        node = data
        for key in path_keys[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                child = {}
                node[key] = child
            node = child
        last = path_keys[-1]
        bucket = node.get(last)
        if not isinstance(bucket, list):
            bucket = []
            node[last] = bucket
        for raw in rows:
            if not isinstance(raw, dict) or not str(raw.get(item_key) or "").strip():
                continue
            ident = str(raw[item_key]).strip()
            target = next(
                (x for x in bucket if isinstance(x, dict) and str(x.get(item_key) or "").strip() == ident),
                None,
            )
            if target is None:
                bucket.append(raw)
                appended += 1
            else:
                target.update(raw)
                upserted += 1
    elif rows:
        if isinstance(data, list):
            data.extend(rows)
            appended = len(rows)
        else:
            for raw in rows:
                if isinstance(raw, dict):
                    data = _deep_merge(data, raw)
                    upserted += 1
    if isinstance(merge, dict):
        data = _deep_merge(data, merge)
        upserted += len(merge)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"written": True, "upserted": upserted, "appended": appended}


def _patch_md(path: Path, spec: dict[str, Any], content: str, mode: str) -> dict[str, Any]:
    text = content if isinstance(content, str) else ""
    if not text.strip():
        raise ValueError("markdown 交付物需要 content")
    current = path.read_text(encoding="utf-8", errors="replace") if path.exists() else _skeleton_content(spec)
    if mode == "append":
        updated = current.rstrip("\n") + "\n\n" + text.strip() + "\n"
    else:
        updated = text if text.lstrip().startswith("#") else f"# {spec.get('title') or Path(spec['name']).stem}\n\n{text}"
    path.write_text(updated, encoding="utf-8")
    return {"written": True, "chars": len(updated)}


def patch(root: Path, specs: list[dict[str, Any]], name: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """按主键回写一个交付物（CSV upsert 行 / JSON upsert 节点 / MD 追加或覆盖）。"""
    clean = _clean_name(name)
    spec = next((s for s in specs if s["name"] == clean), None)
    if spec is None:
        known = ", ".join(s["name"] for s in specs) or "（无）"
        raise ValueError(f"未声明的交付物「{name}」；本次任务可回写的交付物：{known}")
    path = submission_dir(root) / spec["name"]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_skeleton_content(spec), encoding="utf-8")
    fmt = spec["format"]
    if fmt == "csv":
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise ValueError("CSV 交付物需要 rows（对象数组，按主键 upsert）")
        result = _patch_csv(path, spec, rows)
    elif fmt == "json":
        rows = payload.get("rows")
        merge = payload.get("merge")
        if not isinstance(rows, list) and not isinstance(merge, dict):
            raise ValueError("JSON 交付物需要 rows（数组，按 itemKey upsert）或 merge（按字段合并的对象）")
        result = _patch_json(path, spec, rows if isinstance(rows, list) else [], merge=merge)
    else:
        result = _patch_md(path, spec, str(payload.get("content") or ""), str(payload.get("mode") or "replace"))
    result.update({"name": spec["name"], "path": f"{DELIVERABLE_DIRNAME}/{spec['name']}", "format": fmt})
    return result


def missing_or_empty(root: Path, specs: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """交付物完整性检查：返回（文件缺失, 仅有骨架无内容）。仅用于收尾警告，不阻断。"""
    missing: list[str] = []
    empty: list[str] = []
    for spec in specs:
        path = submission_dir(root) / spec["name"]
        if not path.exists():
            missing.append(spec["name"])
            continue
        try:
            if spec["format"] == "csv":
                rows, _ = _read_csv(path, list(spec.get("columns") or []))
                if not any(any(str(v).strip() for v in row.values()) for row in rows):
                    empty.append(spec["name"])
            elif spec["format"] == "json":
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
                if not data or data == spec.get("skeleton"):
                    empty.append(spec["name"])
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
                if len(text.strip().splitlines()) <= 2:
                    empty.append(spec["name"])
        except (OSError, json.JSONDecodeError, csv.Error):
            empty.append(spec["name"])
    return missing, empty
