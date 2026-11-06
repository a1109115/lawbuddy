"""文书生成模块（DEV-MOD-007/008/009 / PRD §15、§16.3、§18.3 / 技术方案 §5.3、§6.3）。

- 解析层（纯函数）：模板变量解析（{{字段}} 与 【字段】）、槽位渲染、
  占位符审计，是 template.* 工具与 Agent 文书子图的内核；
- 数据层：模板（导入解析失败留痕 REQ-TPL-004；版本组更新/比较/回退 REQ-TPL-005，
  回退仅移动当前版本指针，较新版本保留）、文书草稿（槽位/提纲/正文）、
  草稿版本（REQ-GEN-009 崩溃可恢复 / REQ-GEN-010 版本可查可比较）；
- 编辑与自动保存（DEV-MOD-009）：正文手动保存落版本（kind=edit），
  自动保存按 30 秒窗口形成恢复点（kind=autosave，窗口内合并更新）。

门禁（REQ-GEN-003/005/007，均在数据层强制）：
- 缺失字段不编造：渲染时未填槽位一律输出「xx」占位符；
- 提纲确认门禁：outline_confirmed=0 时拒绝渲染正文；
- 占位符阻断正式导出：blocking_count>0 时仅允许草稿标识导出。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from . import audit
from . import editor
from .storage import get_db
from .workspace import now_iso


class DocGenError(RuntimeError):
    pass


# 草稿版本类型（DEV-MOD-009）：渲染 / 手动编辑 / 自动保存恢复点 / 恢复历史版本 / AI 修订
VERSION_KINDS = ("render", "edit", "autosave", "restore", "ai_revise")
VERSION_KIND_LABELS = {
    "render": "生成", "edit": "编辑", "autosave": "自动保存",
    "restore": "恢复", "ai_revise": "AI 修订",
}
MANUAL_KINDS = ("render", "edit", "restore")


# 槽位值来源三分类（REQ-GEN-004）：案件原文（需引证）/ 用户确认 / 待核实
SOURCE_TYPES = ("case_evidence", "user_confirmed", "pending_verify")
SOURCE_TYPE_LABELS = {
    "case_evidence": "案件原文",
    "user_confirmed": "用户确认",
    "pending_verify": "待核实",
}
DOC_MODES = ("strict", "reference")

# 模板变量语法：{{字段}} 与 【字段】（排除旧版占位符「【待补充 …】」）
_VAR_DOUBLE = re.compile(r"\{\{\s*([^{}\n]+?)\s*\}\}")
_VAR_CN = re.compile(r"【((?!待补充)[^【】\n]{1,24})】")
# 正文未填写占位（用户可见的统一记号 xx；兼容旧版 xxx 与【待补充 字段名】）
_PLACEHOLDER = re.compile(
    r"【待补充\s*([^】]+?)\s*】|(?<![A-Za-z0-9_])x{2,3}(?![A-Za-z0-9_])"
)


def placeholder_of(field_name: str) -> str:
    """未填写字段的正文占位：统一用 xx（可被用户直接查找替换，且不编造事实）。"""
    return "xx"


# ===========================================================================
# 解析层：模板变量解析与渲染（template.search / template.validate / render 内核）
# ===========================================================================

def parse_template_vars(content_md: str) -> list[dict[str, Any]]:
    """提取模板变量并去重（保持出现顺序），返回 [{name, required}]。

    解析失败抛 DocGenError 并标记行号（REQ-TPL-004：保留原文件并标记位置）。
    """
    if not content_md.strip():
        raise DocGenError("模板内容为空")
    found: list[str] = []
    for lineno, line in enumerate(content_md.splitlines(), start=1):
        if line.count("{{") != line.count("}}"):
            raise DocGenError(f"第 {lineno} 行存在未闭合的字段占位（{{ 与 }} 不配对）")
        for m in _VAR_DOUBLE.finditer(line):
            name = m.group(1).strip()
            if name and name not in found:
                found.append(name)
        for m in _VAR_CN.finditer(line):
            name = m.group(1).strip()
            if name and name not in found:
                found.append(name)
    return [{"name": name, "required": False} for name in found]


def render_content(content_md: str, slots: dict[str, dict[str, Any]]) -> str:
    """确定性渲染：模板固定文字原样保留（REQ-GEN-006 锁定），槽位替换为已填值；
    未填槽位输出占位符（REQ-GEN-003 不编造）。"""
    def _sub(m: re.Match[str]) -> str:
        raw = next((g for g in m.groups() if g is not None), "")
        name = raw.strip()
        slot = slots.get(name)
        value = str(slot.get("value") or "").strip() if slot else ""
        return value if value else placeholder_of(name)

    out = _VAR_DOUBLE.sub(_sub, content_md)
    out = _VAR_CN.sub(_sub, out)
    return out


def audit_placeholders(body_md: str) -> list[dict[str, Any]]:
    """占位符审计（REQ-GEN-007）：统计正文中阻断级占位符（缺失字段）。"""
    counts: dict[str, int] = {}
    for m in _PLACEHOLDER.finditer(body_md):
        # 新占位 xx 无字段名：按「未填写」归组；旧数据仍保留原字段名
        name = (m.group(1) or "未填写").strip()
        counts[name] = counts.get(name, 0) + 1
    return [{"name": name, "count": count} for name, count in counts.items()]


def _blocking_total(blocking: list[dict[str, Any]]) -> int:
    """占位符阻断计数：按出现次数统计（xx 统一归组后仍应计满处数）。"""
    return sum(int(item.get("count") or 0) for item in blocking)


def outline_from_template(content_md: str) -> list[dict[str, Any]]:
    """本地降级提纲：从模板标题结构确定性推导（不编造内容，points 留空）。

    识别三类标题行：# 标题；「一、」「第X章」短标题（长度受限、不以句末标点
    结尾、不含模板变量，避免把诉讼请求等内容行误当章节）；「标签：」独占行
    （如「诉讼请求：」）。落款类标签（此致/具状人等）与其余内容行跳过。
    """
    _NON_SECTION_LABELS = {"此致", "具状人", "起诉人", "申请人", "附件", "附", "备注"}
    items: list[dict[str, Any]] = []
    for line in content_md.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading: str | None = None
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
        else:
            m = re.match(
                r"^((?:[一二三四五六七八九十百]+、)|(?:第[零〇一二三四五六七八九十百千\d]+[章节条]))\s*(\S.*)?$",
                stripped,
            )
            if m:
                candidate = (m.group(1) + (m.group(2) or "").strip()).strip()
                if len(candidate) <= 24 and not candidate.endswith(("。", "；", "，", "、")):
                    heading = candidate
            elif stripped.endswith("：") and "{{" not in stripped and len(stripped) <= 14:
                label = stripped.rstrip("：")
                if label not in _NON_SECTION_LABELS:
                    heading = label
        if heading and "{{" not in heading and "【" not in heading \
                and all(it["heading"] != heading for it in items):
            items.append({"heading": heading[:40], "points": "", "sourceDoc": ""})
    if not items:
        items.append({"heading": "正文", "points": "", "sourceDoc": ""})
    return items


# ===========================================================================
# 数据层：模板
# ===========================================================================

def _row_to_template(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "caseId": row["case_id"],
        "rootId": row["root_id"] or row["id"],
        "title": row["title"],
        "docType": row["doc_type"],
        "templateMode": row["template_mode"],
        "contentMd": row["content_md"],
        "fields": _loads(row["fields_json"], []),
        "sourceName": row["source_name"],
        "sourceSha256": row["source_sha256"],
        "status": row["status"],
        "parseError": row["parse_error"],
        "versionNo": row["version_no"],
        "isCurrent": bool(row["is_current"]),
        # DOCX 自定义模板（升级方案 §6.4）：来源格式、原件位置、字段映射与格式警告
        "sourceFormat": row["source_format"] if "source_format" in row.keys() else "md",
        "sourceBlobUri": row["source_blob_uri"] if "source_blob_uri" in row.keys() else None,
        "sourceSizeBytes": row["source_size_bytes"] if "source_size_bytes" in row.keys() else None,
        "fieldMapping": _loads(
            row["field_mapping_json"] if "field_mapping_json" in row.keys() else None, []
        ),
        "formatWarnings": _loads(
            row["format_warnings_json"] if "format_warnings_json" in row.keys() else None, []
        ),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def import_template(
    md_text: str,
    title: str,
    doc_type: str,
    case_id: str | None = None,
    source_name: str | None = None,
    template_mode: str = "strict",
    base_template_id: str | None = None,
    *,
    source_format: str = "md",
    source_blob_uri: str | None = None,
    source_size_bytes: int | None = None,
    field_mapping: list[dict[str, Any]] | None = None,
    format_warnings: list[str] | None = None,
    source_sha256: str | None = None,
) -> dict[str, Any]:
    """导入并解析模板（REQ-TPL-001~004）：解析失败保留原文并标记 status=parse_failed。

    base_template_id 给定时为「更新模板」（REQ-TPL-005）：解析成功则成为该模板
    版本组的新当前版本（version_no 递增、当前指针前移，历史版本全部保留）；
    归属（case_id）与模式继承版本组，入参 case_id/template_mode 不改变组范围。
    解析失败不进入版本组，独立留痕（REQ-TPL-004），既有版本组不受影响。

    DOCX 自定义模板（升级方案 §6.1/§6.4）额外携带：来源格式、原件 blob 位置与大小、
    字段位置映射、格式警告。原件哈希以 source_sha256 为准（DOCX 为字节哈希，
    与 Markdown 的文本哈希区分），保证历史草稿可回溯到确切模板版本。
    """
    if not title.strip():
        raise DocGenError("模板标题不能为空")
    if not doc_type.strip():
        raise DocGenError("文书类型不能为空")
    if template_mode not in DOC_MODES:
        raise DocGenError(f"模板模式不合法：{template_mode}")
    if source_format not in ("md", "txt", "docx"):
        raise DocGenError(f"不支持的模板来源格式：{source_format}")
    base = get_template(base_template_id) if base_template_id else None
    if base is not None and base["status"] == "parse_failed":
        raise DocGenError("所选模板解析失败，不能在其版本组上更新；请修正内容后作为新模板导入")

    md_text = md_text.replace("\r\n", "\n")
    fields: list[dict[str, Any]] = []
    parse_error: str | None = None
    try:
        fields = parse_template_vars(md_text)
    except DocGenError as exc:
        parse_error = str(exc)

    ts = now_iso()
    tpl_id = str(uuid.uuid4())
    sha = source_sha256 or _sha256(md_text)
    mapping_json = _json(field_mapping or [])
    warnings_json = _json(format_warnings or [])
    db = get_db()
    if parse_error is None and base is not None:
        # 版本化更新：组内恰好一个 is_current=1（回退/更新都只移动指针，行不删除）
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) AS v FROM docgen_templates WHERE root_id = ?",
                (base["rootId"],),
            ).fetchone()
            version_no = row["v"] + 1
            conn.execute(
                "UPDATE docgen_templates SET is_current = 0 WHERE root_id = ?", (base["rootId"],)
            )
            conn.execute(
                """
                INSERT INTO docgen_templates(
                  id, case_id, title, doc_type, template_mode, content_md, fields_json,
                  source_name, source_sha256, status, parse_error, version_no, root_id,
                  is_current, created_at, updated_at,
                  source_format, source_blob_uri, source_size_bytes,
                  field_mapping_json, format_warnings_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (tpl_id, base["caseId"], title.strip(), doc_type.strip(), base["templateMode"],
                 md_text, _json(fields), source_name, sha, version_no,
                 base["rootId"], ts, ts, source_format, source_blob_uri, source_size_bytes,
                 mapping_json, warnings_json),
            )
            audit.append(
                conn,
                action="template.version_create",
                resource_type="docgen_template",
                resource_id=tpl_id,
                case_id=base["caseId"],
                metadata={"rootId": base["rootId"], "versionNo": version_no,
                          "prevVersionNo": base["versionNo"], "charCount": len(md_text),
                          "sourceFormat": source_format},
            )
        return get_template(tpl_id)

    status = "active" if parse_error is None else "parse_failed"
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO docgen_templates(
              id, case_id, title, doc_type, template_mode, content_md, fields_json,
              source_name, source_sha256, status, parse_error, version_no, root_id,
              is_current, created_at, updated_at,
              source_format, source_blob_uri, source_size_bytes,
              field_mapping_json, format_warnings_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tpl_id, case_id, title.strip(), doc_type.strip(), template_mode, md_text,
             _json(fields), source_name, sha, status, parse_error,
             tpl_id, ts, ts, source_format, source_blob_uri, source_size_bytes,
             mapping_json, warnings_json),
        )
    return get_template(tpl_id)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def import_docx_template(
    data: bytes,
    title: str,
    doc_type: str,
    *,
    case_id: str | None = None,
    source_name: str | None = None,
    template_mode: str | None = None,
    base_template_id: str | None = None,
) -> dict[str, Any]:
    """导入用户上传的 .docx 文书模板（升级方案 §6.1 / §10 阶段三）。

    - 原件只读保存到工作空间内容存储，写入后不可被生成流程覆盖；
    - 解析正文/表格/页眉页脚中的显式变量，未检测到变量时按 reference 模式导入
      （不阻止导入，由 AI 按范本结构生成）；
    - 上传文件不进入案件材料库，不与案件文档共用生命周期。
    """
    from . import docgen_docx
    from .contract import _store_blob_bytes

    if not title.strip():
        raise DocGenError("模板标题不能为空")
    if not doc_type.strip():
        raise DocGenError("文书类型不能为空")
    if not data:
        raise DocGenError("模板文件为空")
    try:
        parsed = docgen_docx.extract_template(data)
    except docgen_docx.DocxTemplateError as exc:
        raise DocGenError(str(exc)) from exc

    if template_mode is not None:
        if template_mode not in DOC_MODES:
            raise DocGenError(f"模板模式不合法：{template_mode}")
    else:
        template_mode = parsed["suggestedMode"]

    from . import documents

    blob_uri = _store_blob_bytes(documents._workspace_root(), data, hashlib.sha256(data).hexdigest())
    return import_template(
        parsed["contentMd"], title, doc_type,
        case_id=case_id, source_name=source_name, template_mode=template_mode,
        base_template_id=base_template_id,
        source_format="docx",
        source_blob_uri=blob_uri,
        source_size_bytes=len(data),
        field_mapping=parsed["fieldMapping"],
        format_warnings=parsed["formatWarnings"],
        source_sha256=hashlib.sha256(data).hexdigest(),
    )


def _read_template_docx(template_id: str) -> bytes:
    """读取模板原件字节（严格只读；缺失时明确报错，不静默重建）。"""
    tpl = get_template(template_id)
    if tpl.get("sourceFormat") != "docx" or not tpl.get("sourceBlobUri"):
        raise DocGenError("该模板不是 DOCX 模板，无法按原格式生成")
    from . import crypto, documents
    from .keyring import get_active_dek

    path = documents._workspace_root() / str(tpl["sourceBlobUri"])
    if not path.exists():
        raise DocGenError("模板原件已缺失，无法按原格式生成；请重新上传模板")
    return crypto.read_maybe_encrypted(path, crypto.derive_file_key(get_active_dek()))


def render_docx(
    draft_id: str,
    body_md: str | None = None,
    *,
    allow_unconfirmed: bool = False,
) -> dict[str, Any]:
    """按 DOCX 模板原格式生成 Word（升级方案 §6.2 / §10 阶段三）。

    - strict：在原 OOXML 上替换变量，非变量文字与样式、页眉页脚、图片、编号保持原样，
      未填值写 `xx`；
    - reference：以模板副本为基底（保留页面设置与页眉页脚）写入生成正文；
    - 结果写入新的加密 blob，模板原件不受影响；同时落一条草稿版本，
      便于版本历史与导出回溯到同一次生成。
    """
    import json as _jsonlib

    from . import docgen_docx
    from .contract import _store_blob_bytes

    row = _require_writable_draft(draft_id)
    if not row["outline_confirmed"] and not allow_unconfirmed:
        raise DocGenError("提纲尚未确认，确认提纲后才能生成正文")
    if not row["template_id"]:
        raise DocGenError("草稿未关联 DOCX 模板，无法按原格式生成")
    base = _read_template_docx(str(row["template_id"]))
    tpl = get_template(str(row["template_id"]))
    slots = _loads(row["slots_json"], {})
    values = {
        name: str((slot or {}).get("value") or "").strip()
        for name, slot in slots.items()
    }
    warnings = list(tpl.get("formatWarnings") or [])

    try:
        if tpl["templateMode"] == "strict":
            data, changed = docgen_docx.render_strict(base, values, placeholder_of(""))
            if changed == 0:
                warnings.append("未在模板中定位到可替换变量，已按原样保留模板文字")
            body_md = body_md if body_md is not None else render_content(str(tpl["contentMd"] or ""), slots)
        else:
            # reference 范本只提供版式与结构参照：示例案件事实不得进入新文书
            # （升级方案 §11.1 U9），因此必须有 AI 生成的正文，绝不回退到模板原文
            if body_md is None:
                body_md = row["body_md"]
            if not (body_md or "").strip():
                raise DocGenError(
                    "参考范本需要先生成正文：请先在对话中生成当前稿，再按 Word 模板导出格式"
                )
            data = docgen_docx.render_reference(base, body_md, title=tpl["title"])
    except docgen_docx.DocxTemplateError as exc:
        raise DocGenError(str(exc)) from exc

    if not (body_md or "").strip():
        raise DocGenError("生成的正文为空")
    blocking = audit_placeholders(body_md)
    from . import documents
    blob_uri = _store_blob_bytes(
        documents._workspace_root(), data, hashlib.sha256(data).hexdigest()
    )
    ts = now_iso()
    db = get_db()
    with db.transaction() as conn:
        last = conn.execute(
            "SELECT MAX(version_no) AS v FROM docgen_draft_versions WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
        version_no = (last["v"] or 0) + 1
        conn.execute(
            """
            INSERT INTO docgen_draft_versions(
              id, draft_id, version_no, body_md, outline_json, slots_json, kind,
              created_at, word_blob_uri)
            VALUES (?, ?, ?, ?, ?, ?, 'render', ?, ?)
            """,
            (str(uuid.uuid4()), draft_id, version_no, body_md,
             row["outline_json"], _jsonlib.dumps(slots, ensure_ascii=False), ts, blob_uri),
        )
        conn.execute(
            """
            UPDATE docgen_drafts
            SET body_md = ?, blocking_count = ?, status = 'draft', updated_at = ?
            WHERE id = ?
            """,
            (body_md, _blocking_total(blocking), ts, draft_id),
        )
    return {
        "draft": get_draft(draft_id),
        "versionNo": version_no,
        "templateMode": tpl["templateMode"],
        "warnings": warnings,
    }


def get_template(template_id: str) -> dict[str, Any]:
    row = get_db().query_one("SELECT * FROM docgen_templates WHERE id = ?", (template_id,))
    if row is None:
        raise DocGenError("模板不存在")
    return _row_to_template(row)


def list_templates(case_id: str | None = None) -> list[dict[str, Any]]:
    """案件模板 + 全局模板（case_id 为 NULL 的模板对所有案件可用）。

    只返回各版本组的当前版本（is_current=1）；历史版本经 list_template_versions 查看。
    """
    rows = get_db().query(
        "SELECT * FROM docgen_templates WHERE is_current = 1 "
        "AND (case_id IS NULL OR case_id = ?) ORDER BY updated_at DESC",
        (case_id or "",),
    )
    return [_row_to_template(r) for r in rows]


def _template_group_rows(root_id: str) -> list[dict[str, Any]]:
    return get_db().query(
        "SELECT * FROM docgen_templates WHERE root_id = ? ORDER BY version_no DESC",
        (root_id,),
    )


def list_template_versions(template_id: str) -> list[dict[str, Any]]:
    """模板版本组历史（REQ-TPL-005）：传组内任意版本的 id 均返回全部版本（新→旧）。"""
    row = get_db().query_one("SELECT root_id FROM docgen_templates WHERE id = ?", (template_id,))
    if row is None:
        raise DocGenError("模板不存在")
    return [
        {
            "id": r["id"],
            "rootId": r["root_id"],
            "versionNo": r["version_no"],
            "title": r["title"],
            "docType": r["doc_type"],
            "templateMode": r["template_mode"],
            "status": r["status"],
            "parseError": r["parse_error"],
            "sourceName": r["source_name"],
            "sourceSha256": r["source_sha256"],
            "charCount": len(r["content_md"] or ""),
            "fields": _loads(r["fields_json"], []),
            "isCurrent": bool(r["is_current"]),
            "createdAt": r["created_at"],
            "updatedAt": r["updated_at"],
        }
        for r in _template_group_rows(row["root_id"])
    ]


def _group_version_row(root_id: str, version_no: int) -> dict[str, Any]:
    row = get_db().query_one(
        "SELECT * FROM docgen_templates WHERE root_id = ? AND version_no = ?",
        (root_id, version_no),
    )
    if row is None:
        raise DocGenError(f"模板版本 v{version_no} 不存在")
    return row


def diff_template_versions(
    template_id: str,
    from_version_no: int,
    to_version_no: int | None = None,
) -> dict[str, Any]:
    """模板版本比较（REQ-TPL-005）：to 为空时与组内当前版本比较。

    差异结构与草稿版本比较一致（editor.diff_texts 行级确定性差异）。
    """
    row = get_db().query_one("SELECT root_id FROM docgen_templates WHERE id = ?", (template_id,))
    if row is None:
        raise DocGenError("模板不存在")
    left = _group_version_row(row["root_id"], from_version_no)["content_md"]
    if to_version_no is None:
        current = get_db().query_one(
            "SELECT version_no, content_md FROM docgen_templates "
            "WHERE root_id = ? AND is_current = 1",
            (row["root_id"],),
        )
        if current is None:  # 组内指针恒有一行，防御损坏数据
            raise DocGenError("版本组缺少当前版本指针")
        right = current["content_md"]
        to_label = f"v{current['version_no']}（当前）"
    else:
        to_row = _group_version_row(row["root_id"], to_version_no)
        right = to_row["content_md"]
        to_label = f"v{to_version_no}"
    return {**editor.diff_texts(left, right), "fromLabel": f"v{from_version_no}", "toLabel": to_label}


def rollback_template(template_id: str, version_no: int) -> dict[str, Any]:
    """回退到指定版本（REQ-TPL-005）：仅移动组内当前版本指针，较新版本保留不删除。

    已是当前版本时幂等返回；回退动作写审计（template.rollback，只记版本号与组规模）。
    """
    row = get_db().query_one("SELECT root_id FROM docgen_templates WHERE id = ?", (template_id,))
    if row is None:
        raise DocGenError("模板不存在")
    target = _group_version_row(row["root_id"], version_no)
    if bool(target["is_current"]):
        return get_template(target["id"])
    with get_db().transaction() as conn:
        group = conn.execute(
            "SELECT id, version_no FROM docgen_templates WHERE root_id = ?", (row["root_id"],)
        ).fetchall()
        prev_current = next((g["version_no"] for g in group if g["id"] != target["id"]
                             and _is_current_id(conn, g["id"])), None)
        conn.execute(
            "UPDATE docgen_templates SET is_current = 0 WHERE root_id = ?", (row["root_id"],)
        )
        conn.execute(
            "UPDATE docgen_templates SET is_current = 1, updated_at = ? WHERE id = ?",
            (now_iso(), target["id"]),
        )
        audit.append(
            conn,
            action="template.rollback",
            resource_type="docgen_template",
            resource_id=target["id"],
            case_id=target["case_id"],
            metadata={"rootId": row["root_id"], "fromVersionNo": prev_current,
                      "toVersionNo": target["version_no"], "groupSize": len(group)},
        )
    return get_template(target["id"])


def _is_current_id(conn: Any, template_id: str) -> bool:
    row = conn.execute(
        "SELECT is_current FROM docgen_templates WHERE id = ?", (template_id,)
    ).fetchone()
    return bool(row and row["is_current"])


def update_template_fields(template_id: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    """用户增删改模板字段（REQ-TPL-003：字段识别预览可编辑）。"""
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for f in fields:
        name = str(f.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        clean.append({"name": name, "required": bool(f.get("required", False))})
    with get_db().transaction() as conn:
        conn.execute(
            "UPDATE docgen_templates SET fields_json = ?, updated_at = ? WHERE id = ?",
            (_json(clean), now_iso(), template_id),
        )
    return get_template(template_id)


def delete_template(template_id: str) -> dict[str, Any]:
    tpl = get_template(template_id)
    with get_db().transaction() as conn:
        conn.execute("DELETE FROM docgen_templates WHERE id = ?", (template_id,))
    return {"deleted": True, "id": tpl["id"]}


# ===========================================================================
# 结构化预览（工作台 / 对话卡）：确定性派生，不调用模型
# ===========================================================================

# A4 与法律文书常用版式（导出 Word 时按此应用；只作为展示与导出参数，不写入正文）
DEFAULT_LAYOUT: dict[str, Any] = {
    "paper": "A4",
    "paperSizeMm": {"width": 210, "height": 297},
    "orientation": "portrait",
    "marginsCm": {"top": 3.7, "bottom": 3.5, "left": 2.8, "right": 2.6},
    "titleStyle": {"fontFamily": "SimSun", "fontSize": "22pt", "bold": True, "align": "center"},
    "bodyStyle": {"fontFamily": "FangSong", "fontSize": "16pt", "lineHeight": "1.5"},
}

_LAYOUT_USAGE_TIPS = (
    "将正文中的 xx 占位符替换为已核实信息，不编造缺失事实",
    "事实、金额、日期与证据须能回溯到案件原文，核对文档与页码",
    "定稿前逐项核实当事人身份、诉讼请求与法律依据",
    "签署前核对当事人名称、受理法院与附件清单",
    "导出 Word 后检查纸张、页边距、分页与签名位置",
)

_PARTY_KEYWORDS = (
    "原告", "被告", "申请人", "被申请人", "上诉人", "被上诉人",
    "答辩人", "第三人", "委托人", "收函人", "当事人", "我方角色",
)
_CAUSE_KEYWORDS = ("案由", "纠纷类型")
_CLAIMS_KEYWORDS = ("诉讼请求", "请求事项", "仲裁请求", "申请事项", "反诉请求")
_FACTS_KEYWORDS = ("事实", "理由")
_EVIDENCE_KEYWORDS = ("证据",)
_TAIL_KEYWORDS = ("提交对象", "受理法院", "附件")


def _has_placeholder(text: str) -> bool:
    """文本是否仍含未替换占位（xx / 旧版 xxx / 【待补充 …】）。"""
    return bool(_PLACEHOLDER.search(text or ""))


def _slot_rows(slots: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(str(name), slot or {}) for name, slot in (slots or {}).items()]


def _slot_value(slot: dict[str, Any]) -> str:
    return str(slot.get("value") or "").strip()


def _match_slots(
    slots: dict[str, Any], keywords: tuple[str, ...]
) -> list[tuple[str, dict[str, Any]]]:
    return [(name, slot) for name, slot in _slot_rows(slots)
            if any(key in name for key in keywords) and _slot_value(slot)]


def _first_source(rows: list[tuple[str, dict[str, Any]]]) -> tuple[str | None, dict[str, Any] | None]:
    """取第一个带引证的槽位来源；没有引证时取第一个已填来源类型。"""
    for _name, slot in rows:
        if slot.get("sourceRef"):
            return str(slot.get("sourceType") or "user_confirmed"), slot.get("sourceRef")
    for _name, slot in rows:
        if slot.get("sourceType"):
            return str(slot.get("sourceType")), None
    return None, None


def _body_heading_sections(body_md: str | None) -> list[tuple[str, str]]:
    """把正文按 `## 标题` 切块，返回 [(标题, 正文)]；无标题时整体作为一节。"""
    lines = (body_md or "").replace("\r\n", "\n").split("\n")
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    buf: list[str] = []
    for line in lines:
        stripped = line.strip()
        match = re.match(r"^#{1,4}\s+(.+)$", stripped)
        if match:
            if heading or any(item.strip() for item in buf):
                sections.append((heading, buf))
            heading = match.group(1).strip()
            buf = []
            continue
        buf.append(line)
    if heading or any(item.strip() for item in buf):
        sections.append((heading, buf))
    return [(title, "\n".join(lines).strip()) for title, lines in sections if "\n".join(lines).strip()]


def _body_match(body_md: str | None, keywords: tuple[str, ...]) -> str:
    matched = [text for heading, text in _body_heading_sections(body_md)
               if any(key in heading for key in keywords)]
    return "\n".join(matched).strip()


def _outline_match(outline: list[dict[str, Any]] | None, keywords: tuple[str, ...]) -> str:
    parts = []
    for item in outline or []:
        heading = str(item.get("heading") or "").strip()
        points = str(item.get("points") or "").strip()
        if heading and any(key in heading for key in keywords):
            parts.append(f"{heading}：{points}" if points else heading)
    return "；".join(parts)


def _tail_value(body_md: str | None, slots: dict[str, Any]) -> str:
    lines = [line.strip() for line in (body_md or "").replace("\r\n", "\n").split("\n") if line.strip()]
    tail = [line for line in lines[-20:] if any(key in line for key in
            ("此致", "具状人", "起诉人", "申请人", "上诉人", "附件", "日期", "签名"))]
    if tail:
        return "\n".join(tail[:8])
    rows = _match_slots(slots, _TAIL_KEYWORDS)
    return "；".join(f"{name}：{_slot_value(slot)}" for name, slot in rows)


def _template_meta(template_id: str | None) -> tuple[str, list[str]]:
    """模板标题与写作检查；模板已删除时返回空值（预览仍可派生）。"""
    if not template_id:
        return "", []
    row = get_db().query_one(
        "SELECT title FROM docgen_templates WHERE id = ?", (template_id,)
    )
    title = str(row["title"]) if row else ""
    checks: list[str] = []
    practice = get_db().query_one(
        "SELECT config_json FROM best_practice_templates WHERE template_id = ?", (template_id,)
    )
    if practice:
        try:
            config = json.loads(practice["config_json"] or "{}")
            for check in config.get("writingChecks") or []:
                text = str(check).strip()
                if text and text not in checks:
                    checks.append(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return title, checks


def _field_names(template_id: str | None) -> list[str]:
    if not template_id:
        return []
    row = get_db().query_one(
        "SELECT fields_json FROM docgen_templates WHERE id = ?", (template_id,)
    )
    if not row:
        return []
    try:
        fields = json.loads(row["fields_json"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(field.get("name")) for field in fields if isinstance(field, dict) and field.get("name")]


def _section(
    key: str,
    label: str,
    value: str,
    *,
    source_type: str | None = None,
    source_ref: dict[str, Any] | None = None,
    slot_names: list[str] | None = None,
) -> dict[str, Any]:
    value = (value or "").strip()
    status = "filled" if value and not _has_placeholder(value) else "pending"
    return {
        "key": key,
        "label": label,
        "value": value or None,
        "status": status,
        "sourceType": source_type,
        "sourceRef": source_ref,
        "slotNames": slot_names or [],
    }


def build_preview(row: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    """派生文档结构、排版规范、使用提示与缺失字段（不调用模型、不落库）。"""
    slots = draft.get("slots") or {}
    outline = draft.get("outline") or []
    body_md = row.get("body_md") or ""
    template_title, template_checks = _template_meta(row.get("template_id"))

    parties = _match_slots(slots, _PARTY_KEYWORDS)
    party_text = "；".join(f"{name}：{_slot_value(slot)}" for name, slot in parties)
    party_source, party_ref = _first_source(parties)

    cause_slots = _match_slots(slots, _CAUSE_KEYWORDS)
    cause_text = "；".join(f"{_slot_value(slot)}" for _name, slot in cause_slots)
    if not cause_text and "·" in template_title:
        cause_text = template_title.split("·", 1)[1].strip()
    cause_source, cause_ref = _first_source(cause_slots)

    claims_slots = _match_slots(slots, _CLAIMS_KEYWORDS)
    claims_text = "；".join(f"{name}：{_slot_value(slot)}" for name, slot in claims_slots)
    if not claims_text:
        claims_text = _body_match(body_md, ("请求",)) or _outline_match(outline, ("请求",))
    claims_source, claims_ref = _first_source(claims_slots)

    facts_slots = _match_slots(slots, _FACTS_KEYWORDS)
    facts_text = "；".join(f"{name}：{_slot_value(slot)}" for name, slot in facts_slots)
    if not facts_text:
        facts_text = _body_match(body_md, _FACTS_KEYWORDS) or _outline_match(outline, _FACTS_KEYWORDS)
    facts_source, facts_ref = _first_source(facts_slots)

    evidence_slots = _match_slots(slots, _EVIDENCE_KEYWORDS)
    evidence_text = "；".join(f"{name}：{_slot_value(slot)}" for name, slot in evidence_slots)
    if not evidence_text:
        evidence_text = _body_match(body_md, _EVIDENCE_KEYWORDS)
    evidence_source, evidence_ref = _first_source(evidence_slots)

    tail_text = _tail_value(body_md, slots)
    tail_slots = _match_slots(slots, _TAIL_KEYWORDS)
    tail_source, tail_ref = _first_source(tail_slots)

    title = str(row.get("doc_type") or "").strip() or template_title or str(row.get("purpose") or "").strip()

    field_names = _field_names(row.get("template_id"))
    filled_names = {name for name, slot in _slot_rows(slots) if _slot_value(slot)}
    missing = [name for name in field_names if name not in filled_names]

    usage: list[str] = []
    for tip in [*template_checks, *_LAYOUT_USAGE_TIPS]:
        if tip and tip not in usage:
            usage.append(tip)

    sections = [
        _section("title", "标题", title, source_type="user_confirmed"),
        _section("parties", "当事人", party_text, source_type=party_source, source_ref=party_ref,
                 slot_names=[name for name, _slot in parties]),
        _section("cause", "案由", cause_text, source_type=cause_source, source_ref=cause_ref,
                 slot_names=[name for name, _slot in cause_slots]),
        _section("claims", "诉讼请求", claims_text, source_type=claims_source, source_ref=claims_ref,
                 slot_names=[name for name, _slot in claims_slots]),
        _section("facts", "事实与理由", facts_text, source_type=facts_source, source_ref=facts_ref,
                 slot_names=[name for name, _slot in facts_slots]),
        _section("evidence", "证据和证据来源", evidence_text, source_type=evidence_source,
                 source_ref=evidence_ref, slot_names=[name for name, _slot in evidence_slots]),
        _section("tail", "尾部", tail_text, source_type=tail_source, source_ref=tail_ref,
                 slot_names=[name for name, _slot in tail_slots]),
    ]

    return {
        "title": title or "未命名文书",
        "sections": sections,
        "layout": DEFAULT_LAYOUT,
        "usageTips": usage,
        "missingFields": missing,
        "blockingCount": int(row.get("blocking_count") or draft.get("blockingCount") or 0),
    }


# ===========================================================================
# 数据层：文书草稿
# ===========================================================================

def _row_to_draft(row: dict[str, Any], *, with_body: bool = True) -> dict[str, Any]:
    tpl = get_db().query_one(
        "SELECT title FROM docgen_templates WHERE id = ?", (row["template_id"],)
    ) if row["template_id"] else None
    out: dict[str, Any] = {
        "id": row["id"],
        "caseId": row["case_id"],
        "taskId": row["task_id"],
        "templateId": row["template_id"],
        "templateTitle": tpl["title"] if tpl else None,
        "templateVersionNo": row["template_version_no"],
        "templateContentSha256": row["template_content_sha256"],
        "docType": row["doc_type"],
        "purpose": row["purpose"],
        "outline": _loads(row["outline_json"], None),
        "outlineConfirmed": bool(row["outline_confirmed"]),
        "slots": _loads(row["slots_json"], {}),
        "status": row["status"],
        "blocking": audit_placeholders(row["body_md"] or "") if row["body_md"] else [],
        "blockingCount": row["blocking_count"],
        # 当前稿版本号（升级方案 §7.4）：消息、文件卡、预览与修订都以它为准，
        # 避免基于旧稿覆盖新稿
        "currentVersionNo": latest_version_no(row["id"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if with_body:
        out["bodyMd"] = row["body_md"]
        out["preview"] = build_preview(row, out)
    return out


def latest_version_no(draft_id: str) -> int:
    """草稿当前稿版本号（无版本时为 0；用于 baseVersionNo 一致性校验）。"""
    row = get_db().query_one(
        "SELECT MAX(version_no) AS v FROM docgen_draft_versions WHERE draft_id = ?",
        (draft_id,),
    )
    return int((row["v"] if row else 0) or 0)


def _require_draft(draft_id: str) -> dict[str, Any]:
    row = get_db().query_one("SELECT * FROM docgen_drafts WHERE id = ?", (draft_id,))
    if row is None:
        raise DocGenError("文书草稿不存在")
    return row


def _require_writable_draft(draft_id: str) -> dict[str, Any]:
    from .cases import require_case_mode

    row = _require_draft(draft_id)
    require_case_mode(row["case_id"], "document_generation")
    return row


def _verify_slot_source(case_id: str, value: str, source: dict[str, Any]) -> dict[str, Any]:
    from . import citations
    from .cases import readable_case_ids

    ids = readable_case_ids(case_id)
    placeholders = ",".join("?" for _ in ids)
    doc = get_db().query_one(
        f"SELECT title, active_version_id, status FROM documents WHERE id = ? AND case_id IN ({placeholders})",
        (source.get("documentId"), *ids),
    )
    page_no, quote = source.get("pageNo"), source.get("quote")
    if not doc or not isinstance(page_no, int) or isinstance(page_no, bool) or page_no < 1:
        raise DocGenError("案件原文引证的文档或页码无法核验")
    if not isinstance(quote, str) or not citations.normalize_text(quote):
        raise DocGenError("案件原文引证必须包含真实原文片段")
    version = source.get("documentVersionId") or source.get("versionId") or doc["active_version_id"]
    if version != doc["active_version_id"]:
        raise DocGenError("案件原文引证版本已失效")
    if not citations.verify_quote(citations.normalize_text(quote), version, page_no):
        raise DocGenError("案件原文引证不匹配该页文本")
    if citations.normalize_text(value) not in citations.normalize_text(quote):
        raise DocGenError("槽位值不能从引证原文确认，请使用用户确认或待核实来源")
    page = get_db().query_one(
        "SELECT ocr_confidence FROM document_pages WHERE version_id = ? AND page_no = ?", (version, page_no),
    )
    if doc["status"] not in ("ready", "partial") or (
        page and page["ocr_confidence"] is not None and page["ocr_confidence"] < citations.LOW_OCR_CONFIDENCE
    ):
        raise DocGenError("引证原文解析质量不足，请人工核实")
    return {**source, "documentVersionId": version, "documentTitle": doc["title"]}


def _source_blockers(row: dict[str, Any]) -> list[str]:
    blockers = []
    slots = _loads(row["slots_json"], {})
    # 模板字段全部可选（用户可直接自然语言起草，未填写以 xx 占位），
    # 不再用 required 阻止定稿；来源不确定/引证失效仍须人工核实。
    for name, slot in slots.items():
        if slot.get("sourceType") == "pending_verify":
            blockers.append(name)
        elif slot.get("sourceType") == "case_evidence":
            try:
                _verify_slot_source(row["case_id"], str(slot.get("value") or ""), slot.get("sourceRef") or {})
            except DocGenError:
                blockers.append(name)
    return blockers


def create_draft(
    case_id: str,
    template_id: str,
    doc_type: str,
    purpose: str,
    task_id: str | None = None,
    mismatch_confirmed: bool = False,
) -> dict[str, Any]:
    """创建文书草稿（提纲待确认状态）。

    - 模板必须 active（parse_failed 的模板不可用，REQ-TPL-004）；
    - 请求事项（purpose）缺失阻止后续正文生成（PRD 15.6 异常降级表）；
    - 模板与文书类型不匹配：需用户确认后才继续（PRD 15.6）。
    """
    if not purpose.strip():
        raise DocGenError("请填写文书用途或请求事项（缺失时无法生成正文）")
    from .cases import require_case_mode

    require_case_mode(case_id, "document_generation")
    tpl = get_template(template_id)
    if tpl["status"] != "active":
        raise DocGenError(
            f"模板解析失败，不可用于生成：{tpl['parseError'] or '未知错误'}"
        )
    if tpl["docType"] != doc_type.strip() and not mismatch_confirmed:
        raise DocGenError(
            f"所选模板为「{tpl['docType']}」模板，与文书类型「{doc_type.strip()}」不匹配，确认后请重试"
        )

    draft_id = str(uuid.uuid4())
    ts = now_iso()
    with get_db().transaction() as conn:
        conn.execute(
            """
            INSERT INTO docgen_drafts(
              id, case_id, task_id, template_id, template_version_no,
              template_content_sha256, doc_type, purpose, slots_json, status,
              created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', 'outline', ?, ?)
            """,
            (draft_id, case_id, task_id, template_id, tpl["versionNo"],
             tpl["sourceSha256"], doc_type.strip(), purpose.strip(), ts, ts),
        )
    return get_draft(draft_id)


# 自由起草的默认章节骨架：按文书类型给出可确认的结构，避免无模板时停摆
_FREE_HEADING_PRESETS = (
    (("起诉状", "上诉状", "答辩状", "反诉状"), ("当事人信息", "请求事项", "事实与理由", "证据与落款")),
    (("律师函",), ("委托与收函主体", "事实与证据", "履行要求与期限", "注意事项与落款")),
    (("意见书",), ("委托与审查范围", "事实及证据", "争议焦点与分析", "结论与待核实事项")),
    (("申请书", "申请"), ("申请人信息", "申请事项", "事实与理由", "证据与落款")),
)
_FREE_HEADINGS_FALLBACK = ("当事人及程序信息", "请求事项", "事实与理由", "证据与落款")


def free_headings(doc_type: str) -> list[str]:
    """按文书类型推导自由起草章节；未知类型回到通用四段结构。"""
    for keywords, headings in _FREE_HEADING_PRESETS:
        if any(keyword in doc_type for keyword in keywords):
            return list(headings)
    return list(_FREE_HEADINGS_FALLBACK)


def create_free_draft(
    case_id: str,
    doc_type: str,
    purpose: str,
    headings: list[str] | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """按章节自由起草：无同名模板时也能产出可确认提纲的草稿。

    同名自由模板按案件复用版本组，避免每轮起草都新建一个模板污染模板列表。
    """
    doc_type = doc_type.strip()
    if not doc_type:
        raise DocGenError("文书类型不能为空")
    titles = [
        str(heading).replace("{", "").replace("}", "").replace("\n", " ").strip()[:60]
        for heading in (headings or [])
    ]
    titles = [title for title in titles if title][:20] or free_headings(doc_type)
    content = "# 自由起草结构\n\n" + "\n\n".join(
        f"## {title}\n{{{{章节{index + 1}内容}}}}" for index, title in enumerate(titles)
    )
    prior = get_db().query_one(
        "SELECT id FROM docgen_templates WHERE is_current = 1 AND case_id = ? "
        "AND title = ? AND doc_type = ? AND source_name IS NULL "
        "ORDER BY updated_at DESC LIMIT 1",
        (case_id, doc_type, doc_type),
    )
    template = import_template(
        content, doc_type, doc_type,
        case_id=case_id, template_mode="reference",
        base_template_id=(prior["id"] if prior else None),
    )
    draft = create_draft(case_id, template["id"], doc_type, purpose, task_id=task_id)
    return update_outline(draft["id"], [
        {"heading": title, "points": "请确认章节内容和事实来源", "sourceDoc": ""} for title in titles
    ])


def get_draft(draft_id: str) -> dict[str, Any]:
    return _row_to_draft(_require_draft(draft_id))


def list_drafts(case_id: str) -> list[dict[str, Any]]:
    rows = get_db().query(
        "SELECT * FROM docgen_drafts WHERE case_id = ? ORDER BY updated_at DESC",
        (case_id,),
    )
    return [_row_to_draft(r, with_body=False) for r in rows]


# ---------------------------------------------------------------------------
# 槽位填充（REQ-GEN-004：来源三分类）
# ---------------------------------------------------------------------------

def fill_slots(draft_id: str, values: list[dict[str, Any]]) -> dict[str, Any]:
    """填充/清空槽位。values: [{name, value, sourceType, sourceRef?}]，value 空串表示清空。

    - sourceType=case_evidence 必须带引证（sourceRef 含 documentId/pageNo/quote），
      保证正文内容可回溯案件原文（REQ-GEN-004）；
    - 未知字段拒绝（防止槽位逃逸到模板固定文字之外）。
    """
    row = _require_writable_draft(draft_id)
    tpl = get_template(row["template_id"]) if row["template_id"] else None
    known = {f["name"] for f in (tpl["fields"] if tpl else [])}
    slots = _loads(row["slots_json"], {})
    for v in values:
        name = str(v.get("name") or "").strip()
        if not name:
            raise DocGenError("槽位字段名不能为空")
        if known and name not in known:
            raise DocGenError(f"字段「{name}」不在模板字段清单中")
        source_type = str(v.get("sourceType") or "user_confirmed")
        if source_type not in SOURCE_TYPES:
            raise DocGenError(f"来源类型不合法：{source_type}")
        value = str(v.get("value") or "").strip()
        if not value:
            slots.pop(name, None)
            continue
        source_ref = v.get("sourceRef") if isinstance(v.get("sourceRef"), dict) else None
        if source_type == "case_evidence":
            if not source_ref or not str(source_ref.get("documentId") or "").strip():
                raise DocGenError(f"字段「{name}」来源为案件原文，必须附引证（文档与页码）")
            source_ref = _verify_slot_source(row["case_id"], value, source_ref)
        else:
            # 用户确认与待核实不是原文证据：即便调用方传了 documentId/quote 也不落库，
            # 否则预览与引证芯片会把用户口述/模型假设显示成可跳转的原文引证。
            source_ref = None
        slots[name] = {"value": value, "sourceType": source_type, "sourceRef": source_ref}

    ts = now_iso()
    with get_db().transaction() as conn:
        conn.execute(
            "UPDATE docgen_drafts SET slots_json = ?, updated_at = ? WHERE id = ?",
            (_json(slots), ts, draft_id),
        )
    return get_draft(draft_id)


# ---------------------------------------------------------------------------
# 提纲（REQ-GEN-005：确认后才生成正文；返回修改提纲 → 正文待重算）
# ---------------------------------------------------------------------------

def update_outline(draft_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """写入/修改提纲。修改提纲后正文待重算：outline_confirmed 复位、正文回退提纲态。"""
    _require_writable_draft(draft_id)
    clean: list[dict[str, Any]] = []
    for it in items:
        heading = str(it.get("heading") or "").strip()
        if not heading:
            continue
        clean.append({
            "heading": heading[:60],
            "points": str(it.get("points") or "").strip(),
            "sourceDoc": str(it.get("sourceDoc") or "").strip(),
        })
    if not clean:
        raise DocGenError("提纲不能为空")
    ts = now_iso()
    with get_db().transaction() as conn:
        conn.execute(
            """
            UPDATE docgen_drafts
            SET outline_json = ?, outline_confirmed = 0, body_md = NULL,
                blocking_count = 0, status = 'outline', updated_at = ?
            WHERE id = ?
            """,
            (_json(clean), ts, draft_id),
        )
    return get_draft(draft_id)


def confirm_outline(draft_id: str) -> dict[str, Any]:
    """确认提纲（REQ-GEN-005 门禁第一步；请求事项缺失阻止继续，PRD 15.6）。"""
    row = _require_writable_draft(draft_id)
    if not (row["purpose"] or "").strip():
        raise DocGenError("请求事项缺失，请先补充文书用途后再确认提纲")
    if not row["outline_json"]:
        raise DocGenError("尚无提纲可确认")
    with get_db().transaction() as conn:
        conn.execute(
            "UPDATE docgen_drafts SET outline_confirmed = 1, updated_at = ? WHERE id = ?",
            (now_iso(), draft_id),
        )
    return get_draft(draft_id)


# ---------------------------------------------------------------------------
# 正文渲染与版本（REQ-GEN-003/005/006/009/010）
# ---------------------------------------------------------------------------

def render_body(
    draft_id: str,
    body_md: str | None = None,
    *,
    allow_unconfirmed: bool = False,
) -> dict[str, Any]:
    """生成正文并落版本。

    - 本地确定性渲染（body_md 为空时）：模板固定文字 + 槽位替换，缺 → 占位符；
    - 模型路径（body_md 给定）：由 node_verify 在提纲已确认时传入生成的草稿正文，
      此处仅做占位符审计与版本落库，不改写模型内容；
    - 门禁：提纲未确认拒绝生成正文（REQ-GEN-005）。allow_unconfirmed 仅供
      AI 起草草稿态渲染使用（首轮直接产出「草稿」Word），正式导出仍走
      status/blocking_count 门禁，用户仍需在文书工作台确认提纲与定稿；
    - 每次渲染写一条草稿版本（REQ-GEN-009 崩溃可恢复 / REQ-GEN-010 可查）。
    """
    row = _require_writable_draft(draft_id)
    if not row["outline_confirmed"] and not allow_unconfirmed:
        raise DocGenError("提纲尚未确认，确认提纲后才能生成正文")
    if body_md is None:
        slots = _loads(row["slots_json"], {})
        tpl = get_template(row["template_id"]) if row["template_id"] else None
        if tpl is None:
            raise DocGenError("草稿关联的模板已删除，无法渲染")
        if tpl.get("sourceFormat") == "docx" and tpl.get("templateMode") == "reference":
            # 参考范本只是版式参照，其正文属于范本示例（升级方案 §11.1 U9）：
            # 不能直接当成新文书正文，必须有 AI 生成内容
            raise DocGenError(
                "参考范本需要先生成正文：请让 AI 依据案件材料起草当前稿，再按 Word 模板导出格式"
            )
        body_md = render_content(str(tpl["contentMd"] or ""), slots)
    if not body_md.strip():
        raise DocGenError("生成的正文为空")

    blocking = audit_placeholders(body_md)
    ts = now_iso()
    db = get_db()
    with db.transaction() as conn:
        last = conn.execute(
            "SELECT MAX(version_no) AS v FROM docgen_draft_versions WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
        version_no = (last["v"] or 0) + 1
        conn.execute(
            """
            INSERT INTO docgen_draft_versions(
              id, draft_id, version_no, body_md, outline_json, slots_json, kind, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'render', ?)
            """,
            (str(uuid.uuid4()), draft_id, version_no, body_md,
             row["outline_json"], row["slots_json"], ts),
        )
        conn.execute(
            """
            UPDATE docgen_drafts
            SET body_md = ?, blocking_count = ?, status = 'draft', updated_at = ?
            WHERE id = ?
            """,
            (body_md, _blocking_total(blocking), ts, draft_id),
        )
    return get_draft(draft_id)


def revise_body(
    draft_id: str,
    body_md: str,
    instruction: str = "",
    *,
    allow_unconfirmed: bool = False,
    base_version_no: int | None = None,
) -> dict[str, Any]:
    """AI 修订落库（交付物迭代改写）：以当前稿为基准的修订正文追加为
    kind='ai_revise' 的新版本，历史版本不删（REQ-GEN-010 同语义）。

    - 修订正文由 agent 任务（document_generation 修订分叉）按用户意见生成，
      此处只做占位符审计与版本落库，不改写内容；
    - allow_unconfirmed：AI 起草的草稿可在用户确认提纲前直接迭代（正文已存在，
      用户仍可在工作台确认提纲并定稿）；
    - base_version_no（升级方案 §7.4）：修订基于的版本锚点。给定时必须等于当前稿
      版本号，否则拒绝落库，避免基于旧稿覆盖用户在编辑期间产生的新稿；
    - 修订使已定稿草稿回到草稿态（REQ-GEN-007 门禁不被 AI 修订绕过）；
    - 返回 {draft, versionNo, baseVersionNo, kind, instruction, savedAt}。
    """
    if not (body_md or "").strip():
        raise DocGenError("修订正文不能为空")
    row = _require_writable_draft(draft_id)
    if not row["outline_confirmed"] and not allow_unconfirmed:
        raise DocGenError("提纲尚未确认，确认提纲后才能修订正文")
    if not (row["body_md"] or "").strip():
        raise DocGenError("草稿尚无正文，请先生成正文再修订")
    current_version = latest_version_no(draft_id)
    if base_version_no is not None and int(base_version_no) != current_version:
        raise DocGenError(
            f"VERSION_CONFLICT：修订基于 v{base_version_no}，而当前稿已是 v{current_version}；"
            "请基于最新稿重新修订，避免覆盖较新的编辑内容"
        )
    blocking = audit_placeholders(body_md)
    ts = now_iso()
    db = get_db()
    with db.transaction() as conn:
        last = conn.execute(
            "SELECT MAX(version_no) AS v FROM docgen_draft_versions WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
        version_no = (last["v"] or 0) + 1
        conn.execute(
            """
            INSERT INTO docgen_draft_versions(
              id, draft_id, version_no, body_md, outline_json, slots_json, kind, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'ai_revise', ?)
            """,
            (str(uuid.uuid4()), draft_id, version_no, body_md,
             row["outline_json"], row["slots_json"], ts),
        )
        conn.execute(
            """
            UPDATE docgen_drafts
            SET body_md = ?, blocking_count = ?, status = 'draft', updated_at = ?
            WHERE id = ?
            """,
            (body_md, _blocking_total(blocking), ts, draft_id),
        )
    return {
        "draft": get_draft(draft_id),
        "versionNo": version_no,
        "baseVersionNo": current_version,
        "kind": "ai_revise",
        "instruction": (instruction or "").strip(),
        "savedAt": ts,
    }


def confirm_draft(draft_id: str, content_reviewed: bool = False,
                  expected_updated_at: str | None = None) -> dict[str, Any]:
    """定稿（REQ-GEN-007：存在阻断级占位符时不能定稿）。"""
    row = _require_writable_draft(draft_id)
    if row["blocking_count"] > 0:
        raise DocGenError(
            f"正文存在 {row['blocking_count']} 处待补充占位符，请先补全或确认缺失后再定稿"
        )
    if _source_blockers(row):
        raise DocGenError("存在未填必需字段、待核实或已失效的事实来源，请核实后再定稿")
    if not (row["body_md"] or "").strip():
        raise DocGenError("尚无正文可定稿")
    latest = get_db().query_one(
        "SELECT kind FROM docgen_draft_versions WHERE draft_id = ? ORDER BY version_no DESC LIMIT 1", (draft_id,),
    )
    if latest and latest["kind"] != "render" and row["status"] != "confirmed":
        if content_reviewed is not True or expected_updated_at != row["updated_at"]:
            raise DocGenError("CONTENT_REVIEW_REQUIRED：正文已编辑或修订，请核实当前版本新增事实和法律依据后确认")
    with get_db().transaction() as conn:
        changed = conn.execute(
            "UPDATE docgen_drafts SET status = 'confirmed', updated_at = ? WHERE id = ? AND updated_at = ?",
            (now_iso(), draft_id, row["updated_at"]),
        )
        if changed.rowcount != 1:
            raise DocGenError("VERSION_CONFLICT")
        from . import audit

        audit.append(conn, action="docgen.confirm", resource_type="docgen_draft", resource_id=draft_id,
                     case_id=row["case_id"], metadata={"contentReviewed": content_reviewed,
                                                       "versionKind": latest["kind"] if latest else None})
    return get_draft(draft_id)


def list_versions(draft_id: str) -> list[dict[str, Any]]:
    rows = get_db().query(
        """
        SELECT version_no, body_md, kind, created_at FROM docgen_draft_versions
        WHERE draft_id = ? ORDER BY version_no DESC
        """,
        (draft_id,),
    )
    return [
        {
            "versionNo": r["version_no"],
            "bodyMd": r["body_md"],
            "kind": r["kind"],
            "kindLabel": VERSION_KIND_LABELS.get(r["kind"], r["kind"]),
            "charCount": len(r["body_md"] or ""),
            "blocking": audit_placeholders(r["body_md"]),
            "createdAt": r["created_at"],
        }
        for r in rows
    ]


def _version_row(draft_id: str, version_no: int) -> dict[str, Any]:
    row = get_db().query_one(
        "SELECT * FROM docgen_draft_versions WHERE draft_id = ? AND version_no = ?",
        (draft_id, version_no),
    )
    if row is None:
        raise DocGenError(f"版本 v{version_no} 不存在")
    return row


def restore_version(draft_id: str, version_no: int) -> dict[str, Any]:
    """恢复到历史版本（REQ-GEN-010）：历史版本追加保留，恢复动作自身也落一条版本。

    恢复后回到草稿态并重新审计占位符；旧版本（含被覆盖的当前稿）不被删除。
    """
    _require_writable_draft(draft_id)
    ver = _version_row(draft_id, version_no)
    blocking = audit_placeholders(ver["body_md"])
    ts = now_iso()
    with get_db().transaction() as conn:
        last = conn.execute(
            "SELECT MAX(version_no) AS v FROM docgen_draft_versions WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO docgen_draft_versions(
              id, draft_id, version_no, body_md, outline_json, slots_json, kind, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'restore', ?)
            """,
            (str(uuid.uuid4()), draft_id, (last["v"] or 0) + 1, ver["body_md"],
             ver["outline_json"], ver["slots_json"], ts),
        )
        conn.execute(
            """
            UPDATE docgen_drafts
            SET body_md = ?, blocking_count = ?, slots_json = ?, outline_json = ?, status = 'draft', updated_at = ?
            WHERE id = ?
            """,
            (ver["body_md"], _blocking_total(blocking), ver["slots_json"], ver["outline_json"], ts, draft_id),
        )
    return get_draft(draft_id)


# ---------------------------------------------------------------------------
# 正文编辑与自动保存（DEV-MOD-009 / PRD §18.3 / REQ-GEN-009）
# ---------------------------------------------------------------------------

def open_editor(draft_id: str) -> dict[str, Any]:
    """进入草稿编辑视图：开启编辑会话（崩溃恢复标记）。"""
    row = _require_writable_draft(draft_id)
    return editor.open_session("docgen_draft", draft_id, row["case_id"])


def close_editor(session_id: str) -> dict[str, Any]:
    return editor.close_session(session_id)


def save_body(
    draft_id: str,
    body_md: str,
    kind: str = "edit",
    session_id: str | None = None,
    *,
    expected_version_no: int | None = None,
) -> dict[str, Any]:
    """用户编辑正文落库（REQ-GEN-009 自动保存与恢复、REQ-GEN-010 版本可查）。

    - kind='edit'：手动保存 → 追加版本（kind='edit'）；
    - kind='autosave'：停止输入 2 秒自动保存（UI 防抖触发）→ 工作稿立即可恢复；
      距最近自动保存恢复点 30 秒内合并更新（同一恢复点取最新文本），否则形成新恢复点；
    - 编辑使已定稿草稿回到草稿态，占位符重新审计（REQ-GEN-007 门禁不被编辑绕过）。

    expected_version_no（升级方案 §7.4 / 阶段二）：编辑器开始编辑时读到的当前稿版本号。
    给定时必须等于当前稿版本号，否则拒绝落库——用户在另一窗口编辑或 AI 修订已产生
    新版本，继续保存会静默覆盖较新的内容。冲突时由 UI 提示重新载入最新稿。
    """
    if kind not in ("edit", "autosave"):
        raise DocGenError(f"不支持的保存类型：{kind}")
    if not body_md.strip():
        raise DocGenError("正文不能为空")
    row = _require_writable_draft(draft_id)
    if not row["outline_confirmed"]:
        raise DocGenError("提纲尚未确认，确认提纲后才能编辑正文")
    blocking = audit_placeholders(body_md)
    ts = now_iso()
    db = get_db()
    with db.transaction() as conn:
        last = conn.execute(
            "SELECT * FROM docgen_draft_versions WHERE draft_id = ? ORDER BY version_no DESC LIMIT 1",
            (draft_id,),
        ).fetchone()
        latest_no = int(last["version_no"] if last else 0)
        if expected_version_no is not None and int(expected_version_no) != latest_no:
            raise DocGenError(
                f"VERSION_CONFLICT：保存基于 v{int(expected_version_no)}，而当前稿已是 v{latest_no}；"
                "为避免覆盖较新的编辑内容，请先载入最新稿再保存"
            )
        version_no = latest_no + 1
        merged = False
        if kind == "autosave" and last is not None and last["kind"] == "autosave" \
                and editor.within_autosave_window(last["created_at"], ts):
            # 同一恢复点内合并更新：保留 created_at 作为窗口起点，连续编辑每 30 秒形成新恢复点
            conn.execute(
                "UPDATE docgen_draft_versions SET body_md = ?, slots_json = ? WHERE id = ?",
                (body_md, row["slots_json"], last["id"]),
            )
            version_no = last["version_no"]
            merged = True
        else:
            conn.execute(
                """
                INSERT INTO docgen_draft_versions(
                  id, draft_id, version_no, body_md, outline_json, slots_json, kind, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), draft_id, version_no, body_md,
                 row["outline_json"], row["slots_json"], kind, ts),
            )
        conn.execute(
            """
            UPDATE docgen_drafts
            SET body_md = ?, blocking_count = ?, status = 'draft', updated_at = ?
            WHERE id = ?
            """,
            (body_md, _blocking_total(blocking), ts, draft_id),
        )
        if session_id:
            editor.mark_autosave(conn, session_id, ts)
    return {
        "draft": get_draft(draft_id),
        "versionNo": version_no,
        "kind": kind,
        "merged": merged,
        "savedAt": ts,
    }


def promote_autosave(draft_id: str) -> dict[str, Any]:
    """崩溃恢复=保留最近自动保存：把最新自动保存恢复点转为普通编辑版本。

    无自动保存版本时返回当前草稿（幂等，不报错）。
    """
    _require_writable_draft(draft_id)
    ts = now_iso()
    db = get_db()
    with db.transaction() as conn:
        last = conn.execute(
            "SELECT * FROM docgen_draft_versions WHERE draft_id = ? ORDER BY version_no DESC LIMIT 1",
            (draft_id,),
        ).fetchone()
        if last is not None and last["kind"] == "autosave":
            conn.execute("UPDATE docgen_draft_versions SET kind = 'edit' WHERE id = ?", (last["id"],))
            conn.execute("UPDATE docgen_drafts SET updated_at = ? WHERE id = ?", (ts, draft_id))
    return get_draft(draft_id)


def discard_autosaves(draft_id: str) -> dict[str, Any]:
    """崩溃恢复=放弃自动保存：删除自动保存恢复点，正文回到最近手动版本。

    无手动版本（正文仅来自自动保存）时不丢失内容：保留并转为编辑版本，避免清空用户已写文本。
    """
    row = _require_writable_draft(draft_id)
    db = get_db()
    manual = db.query_one(
        """
        SELECT * FROM docgen_draft_versions
        WHERE draft_id = ? AND kind IN ('render', 'edit', 'restore')
        ORDER BY version_no DESC LIMIT 1
        """,
        (draft_id,),
    )
    ts = now_iso()
    body = (manual["body_md"] if manual else row["body_md"]) or ""
    blocking = audit_placeholders(body)
    with db.transaction() as conn:
        if manual is None:
            # 仅自动保存的正文转正（放弃恢复不能丢失用户已写内容）；更早的恢复点保留
            conn.execute(
                """
                UPDATE docgen_draft_versions SET kind = 'edit'
                WHERE draft_id = ? AND kind = 'autosave'
                  AND version_no = (SELECT MAX(version_no) FROM docgen_draft_versions
                                    WHERE draft_id = ? AND kind = 'autosave')
                """,
                (draft_id, draft_id),
            )
        else:
            conn.execute(
                "DELETE FROM docgen_draft_versions WHERE draft_id = ? AND kind = 'autosave'",
                (draft_id,),
            )
        conn.execute(
            """
            UPDATE docgen_drafts
            SET body_md = ?, blocking_count = ?, status = 'draft', updated_at = ?
            WHERE id = ?
            """,
            (body, _blocking_total(blocking), ts, draft_id),
        )
    return get_draft(draft_id)


def diff_versions(
    draft_id: str,
    from_version_no: int,
    to_version_no: int | None = None,
) -> dict[str, Any]:
    """版本比较（REQ-GEN-010 / REQ-CON-002）：to 为空时与当前稿比较。"""
    _require_draft(draft_id)
    left = _version_row(draft_id, from_version_no)["body_md"]
    if to_version_no is None:
        row = _require_draft(draft_id)
        right = row["body_md"] or ""
        to_label = "当前稿"
    else:
        right = _version_row(draft_id, to_version_no)["body_md"]
        to_label = f"v{to_version_no}"
    return {**editor.diff_texts(left, right), "fromLabel": f"v{from_version_no}", "toLabel": to_label}


# ---------------------------------------------------------------------------
# 导出（REQ-GEN-007：阻断级占位符阻止正式导出，仅允许草稿标识导出）
# ---------------------------------------------------------------------------

def export_draft(
    draft_id: str,
    dest_dir: str,
    fmt: str = "md",
    *,
    name_suffix: str = "",
) -> dict[str, Any]:
    """导出文书（G-008：md / txt / docx / pdf / xlsx / pptx；正式导出带完整版本元数据）。

    草稿态（未定稿或存在阻断级占位符）导出文件名加「草稿_」前缀，
    杜绝与正式文书混淆（REQ-GEN-007）。name_suffix 用于任务内自动落盘时
    追加版本号，避免迭代稿互相覆盖。
    """
    row = _require_draft(draft_id)
    if not (row["body_md"] or "").strip():
        raise DocGenError("尚无正文可导出")
    if fmt not in ("md", "txt", "docx", "pdf", "xlsx", "pptx"):
        raise DocGenError(f"不支持的导出格式：{fmt}")

    dest = Path(dest_dir)
    if not dest.is_dir():
        raise DocGenError(f"导出目录不存在：{dest_dir}")

    safe_title = re.sub(r'[\\/:*?"<>|]+', "_", (row["purpose"] or row["doc_type"]).strip())[:40]
    safe_suffix = re.sub(r'[\\/:*?"<>|]+', "_", (name_suffix or "").strip())
    is_formal = row["status"] == "confirmed" and row["blocking_count"] == 0 and not _source_blockers(row)
    if is_formal:
        filename = f"{safe_title}{safe_suffix}.{fmt}"
    else:
        # 草稿标识导出：文件名加「草稿」前缀，杜绝与正式文书混淆
        filename = f"草稿_{safe_title}{safe_suffix}.{fmt}"
    path = dest / filename
    if fmt in ("md", "txt"):
        path.write_text(row["body_md"], encoding="utf-8")
    elif fmt == "docx" and (word_version := get_db().query_one(
        "SELECT word_blob_uri, body_md FROM docgen_draft_versions WHERE draft_id = ? ORDER BY version_no DESC LIMIT 1",
        (draft_id,),
    )) and word_version["word_blob_uri"] and word_version["body_md"] == row["body_md"]:
        from . import crypto, documents
        from .keyring import get_active_dek

        blob = documents._workspace_root() / word_version["word_blob_uri"]
        path.write_bytes(crypto.decrypt_bytes(blob, crypto.derive_file_key(get_active_dek())))
    else:
        from .render import (
            blocks_to_docx,
            blocks_to_pdf,
            blocks_to_pptx,
            blocks_to_xlsx,
            md_to_blocks,
        )

        blocks = md_to_blocks(row["body_md"], title=safe_title)
        if fmt == "docx":
            blocks_to_docx(blocks, str(path))
        elif fmt == "xlsx":
            blocks_to_xlsx(blocks, str(path))
        elif fmt == "pptx":
            blocks_to_pptx(blocks, str(path))
        else:
            blocks_to_pdf(blocks, str(path))
    # 导出来源版本可追溯：记录本次导出对应的草稿版本号（导出即当前稿），
    # 审计留痕（不含正文与 destPath，避免敏感内容入日志）
    ver_row = get_db().query_one(
        "SELECT MAX(version_no) AS v FROM docgen_draft_versions WHERE draft_id = ?",
        (draft_id,),
    )
    source_version_no = (ver_row["v"] or 0) if ver_row else 0
    try:
        from . import audit as audit_log

        with get_db().transaction() as conn:
            audit_log.append(
                conn, action="docgen.export", resource_type="docgen_draft",
                resource_id=draft_id, case_id=row["case_id"],
                metadata={
                    "format": fmt, "sourceVersionNo": source_version_no,
                    "draftMarked": not is_formal,
                },
            )
    except Exception:
        pass  # 审计写失败不阻断导出（文件已生成）
    return {
        "path": str(path),
        "format": fmt,
        "draftMarked": not is_formal,
        "blockingCount": row["blocking_count"],
        "versionNo": source_version_no,
    }


def export_to_workspace(draft_id: str, fmt: str = "docx") -> dict[str, Any]:
    """任务内自动落盘到工作空间根目录（气泡底部文件卡的数据来源）。

    文件名追加当前草稿版本号（_vN），迭代修订不覆盖历史稿；草稿态仍带
    「草稿_」前缀，正式导出仍须按 REQ-GEN-007 门禁走 docgen.export。
    """
    from .sandbox import workspace_root

    ver_row = get_db().query_one(
        "SELECT MAX(version_no) AS v FROM docgen_draft_versions WHERE draft_id = ?",
        (draft_id,),
    )
    version_no = int((ver_row["v"] if ver_row else 0) or 0)
    suffix = f"_v{version_no}" if version_no else ""
    return export_draft(draft_id, str(workspace_root()), fmt, name_suffix=suffix)


# ---------------------------------------------------------------------------
# 序列化工具
# ---------------------------------------------------------------------------

def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _loads(s: str | None, default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return default
