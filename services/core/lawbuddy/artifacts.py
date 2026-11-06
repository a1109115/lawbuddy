"""阅卷产物落库与导出（M2 收尾 / REQ-DOC-005 基础）。

- artifacts 表持久化任务产物（node_verify 产出的结构化 body），
  产物概览与阅读视图以真实数据替换静态 mock；
- 导出：body → 块序列（h1/h2/p/li/table）→ Markdown / DOCX / PDF / XLSX / PPTX 渲染器；
- 导出目标路径由 Electron Main 的保存对话框产生（路径不进 Renderer），
  导出动作写审计（不含 destPath，防路径泄漏入审计日志）。
"""

from __future__ import annotations

import io
import json
import re
import uuid
from pathlib import Path
from typing import Any

from .storage import get_db
from .workspace import now_iso

FORMATS = ("markdown", "docx", "pdf", "xlsx", "pptx", "html", "txt", "csv", "json")

# 调试/临时文件名模式（resp.txt、header.txt、debug.log 之类的工具转储残留）。
# 只拦明显无语义的调试名，不碰正常命名交付物；已入库数据在列表侧同样过滤。
_TEMP_FILE_TITLE_RE = re.compile(
    r"^(?:tmp|temp)[_.\-]|"
    r"^(?:resp|response|headers?|debug|dump|logs?|stdout|stderr|"
    r"output|request|payload|curl|sample|test(?:ing)?|playground|scratch)"
    r"(?:[_.\-#\s\d]|$)",
    re.IGNORECASE,
)


def is_temp_file_title(title: str) -> bool:
    """判定文件产物名是否为调试/临时残留（登记侧跳过、概览侧隐藏）。"""
    return bool(_TEMP_FILE_TITLE_RE.match(str(title or "").strip()))

# 格式 → render 渲染函数（二进制格式共用块序列渲染器）
_RENDER_BY_FMT = {
    "docx": "blocks_to_docx",
    "pdf": "blocks_to_pdf",
    "xlsx": "blocks_to_xlsx",
    "pptx": "blocks_to_pptx",
}


class ArtifactError(RuntimeError):
    pass


def save_artifact(
    task_id: str | None,
    case_id: str,
    kind: str,
    title: str,
    body: dict[str, Any],
    group_id: str | None = None,
) -> str:
    """落库一条产物（node_verify / 收尾登记调用；失败抛错由调用方决定是否阻断）。

    - task_id 可空：孤儿文件补登记（register_orphan_files）无任务归属；
    - group_id 给定时归入既有版本组（同组即同一交付物的历史版本，
      version_no 组内递增，组内最新为当前稿）；缺省自成一组（version_no=1）。
    """
    db = get_db()
    artifact_id = str(uuid.uuid4())
    gid = group_id or artifact_id
    with db.transaction() as conn:
        last = conn.execute(
            "SELECT MAX(version_no) AS v FROM artifacts WHERE group_id = ?", (gid,)
        ).fetchone()
        version_no = (last["v"] or 0) + 1 if group_id else 1
        conn.execute(
            """
            INSERT INTO artifacts(id, task_id, case_id, kind, title, body_json, created_at,
                                  group_id, version_no)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (artifact_id, task_id, case_id, kind, title.strip() or "未命名产物",
             json.dumps(body, ensure_ascii=False), now_iso(), gid, version_no),
        )
    return artifact_id


def save_artifact_version(
    artifact_id: str,
    body: dict[str, Any],
    title: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """在既有产物的版本组内追加新版本（交付物迭代改写）。

    - 组、案件与 kind 沿用原产物；title 缺省沿用原产物标题；
    - 新版本即组内当前稿；历史版本保留不删（可 list_versions 回看/回退）；
    - 写审计（artifact.save_version，metadata 不含 body 原文）。
    """
    db = get_db()
    src = db.query_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
    if src is None:
        raise ArtifactError("产物不存在")
    new_id = str(uuid.uuid4())
    ts = now_iso()
    with db.transaction() as conn:
        last = conn.execute(
            "SELECT MAX(version_no) AS v FROM artifacts WHERE group_id = ?",
            (src["group_id"],),
        ).fetchone()
        version_no = (last["v"] or 0) + 1
        conn.execute(
            """
            INSERT INTO artifacts(id, task_id, case_id, kind, title, body_json, created_at,
                                  group_id, version_no)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id, task_id, src["case_id"], src["kind"],
             (title or src["title"]).strip() or src["title"],
             json.dumps(body, ensure_ascii=False), ts, src["group_id"], version_no),
        )
        from . import audit

        audit.append(
            conn, action="artifact.save_version", resource_type="artifact",
            resource_id=new_id, case_id=src["case_id"],
            metadata={"groupId": src["group_id"], "versionNo": version_no,
                      "fromVersionNo": src["version_no"]},
        )
    return get_artifact(new_id)


def list_artifact_versions(artifact_id: str) -> list[dict[str, Any]]:
    """版本组历史（artifact.list_versions）：组内全部版本按版本号倒序；
    传组内任意版本的 id 均按组返回。"""
    db = get_db()
    src = db.query_one("SELECT group_id FROM artifacts WHERE id = ?", (artifact_id,))
    if src is None:
        raise ArtifactError("产物不存在")
    rows = db.query(
        """
        SELECT id, version_no, kind, title, created_at, task_id FROM artifacts
        WHERE group_id = ? ORDER BY version_no DESC
        """,
        (src["group_id"],),
    )
    return [
        {
            "id": r["id"], "versionNo": r["version_no"], "kind": r["kind"],
            "title": r["title"], "taskId": r["task_id"], "createdAt": r["created_at"],
        }
        for r in rows
    ]


def restore_artifact_version(artifact_id: str, version_no: int) -> dict[str, Any]:
    """回退到组内历史版本：复制该版本内容追加为新当前版本（历史不删，
    与 docgen.restore_version / 模板指针回退同语义，写审计留痕）。"""
    db = get_db()
    src = db.query_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
    if src is None:
        raise ArtifactError("产物不存在")
    ver = db.query_one(
        "SELECT * FROM artifacts WHERE group_id = ? AND version_no = ?",
        (src["group_id"], version_no),
    )
    if ver is None:
        raise ArtifactError(f"版本 v{version_no} 不存在")
    new_id = str(uuid.uuid4())
    ts = now_iso()
    with db.transaction() as conn:
        last = conn.execute(
            "SELECT MAX(version_no) AS v FROM artifacts WHERE group_id = ?",
            (src["group_id"],),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO artifacts(id, task_id, case_id, kind, title, body_json, created_at,
                                  group_id, version_no)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id, src["task_id"], src["case_id"], src["kind"], ver["title"],
             ver["body_json"], ts, src["group_id"], (last["v"] or 0) + 1),
        )
        from . import audit

        audit.append(
            conn, action="artifact.restore_version", resource_type="artifact",
            resource_id=new_id, case_id=src["case_id"],
            metadata={"groupId": src["group_id"], "restoredVersionNo": version_no},
        )
    return get_artifact(new_id)


def list_artifacts(case_id: str) -> list[dict[str, Any]]:
    """产物概览数据源（轻量：不含 body；文件类产物附带 sizeBytes）。

    版本组语义：每组只返回组内最新版本（当前稿），附 groupId/versionNo/versionCount；
    组按最近更新时间倒序。只含交付物：chat（对话分析随会话展示）、调试/临时文件
    残留与被用户移除（dismissed）的产物不进概览。
    """
    db = get_db()
    rows = db.query(
        """
        SELECT a.id, a.task_id, a.case_id, a.kind, a.title, a.created_at, a.body_json,
               a.group_id, a.version_no,
               (SELECT COUNT(*) FROM artifacts g WHERE g.group_id = a.group_id) AS version_count,
               (SELECT MAX(g.created_at) FROM artifacts g WHERE g.group_id = a.group_id) AS group_updated_at
        FROM artifacts a
        JOIN (
          SELECT group_id, MAX(version_no) AS max_no
          FROM artifacts WHERE case_id = ? GROUP BY group_id
        ) cur ON cur.group_id = a.group_id AND cur.max_no = a.version_no
        WHERE a.case_id = ? AND a.dismissed = 0 AND a.kind != 'chat'
          AND a.title NOT LIKE '%对话分析%'
        ORDER BY group_updated_at DESC
        """,
        (case_id, case_id),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        if r["kind"] == "file" and is_temp_file_title(r["title"]):
            # 历史误登记的调试/临时文件（resp.txt 等）不在概览中回显
            continue
        item: dict[str, Any] = {
            "id": r["id"], "taskId": r["task_id"], "caseId": r["case_id"],
            "kind": r["kind"], "title": r["title"], "createdAt": r["created_at"],
            "groupId": r["group_id"], "versionNo": r["version_no"],
            "versionCount": r["version_count"],
        }
        # 文件类产物：body 极小（文件名/大小/位置说明），安全提取大小供文件卡展示
        if r["kind"] == "file":
            try:
                payload = json.loads(r["body_json"] or "{}")
            except (ValueError, TypeError):
                payload = {}
            # 旧版孤儿补登记（task_id 为空、归属只是「近似当前案件」）的概览残留：
            # 工作空间全案件共享，这类归属不可靠（会把文书生成任务的产物算到阅卷
            # 任务上）。不再展示，由 register_orphan_files 按任务时间窗重新登记带
            # taskId 的正确记录；工具内主动登记（docgenDraftId/editableCopy）不受影响。
            if (
                r["task_id"] is None
                and "workspaceDirName" in payload
                and not payload.get("docgenDraftId")
                and payload.get("editableCopy") is not True
            ):
                continue
            size = payload.get("sizeBytes")
            if isinstance(size, (int, float)):
                item["sizeBytes"] = int(size)
            # 文书生成落盘的 Word：前端据此把文件卡点击路由到文书工作台
            draft_id = payload.get("docgenDraftId")
            if isinstance(draft_id, str) and draft_id:
                item["docgenDraftId"] = draft_id
                # 版本锚点（升级方案 §7.4）：文件卡标注对应草稿版本号，
                # 与消息卡、工作台、版本历史使用同一编号
                ver = payload.get("versionNo")
                if isinstance(ver, (int, float)) and int(ver) > 0:
                    item["docgenVersionNo"] = int(ver)
            if payload.get("draftMarked") is True:
                item["draftMarked"] = True
        out.append(item)
    return out


def dismiss_artifact(artifact_id: str) -> dict[str, Any]:
    """用户移除产物（概览不再显示；数据与磁盘文件保留，可审计留痕）。

    整组隐藏：同组历史版本一并置 dismissed，防止概览通过版本接口露回。
    写审计（artifact.dismiss，metadata 不含标题以外的内容信息）。
    """
    db = get_db()
    src = db.query_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
    if src is None:
        raise ArtifactError("产物不存在")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE artifacts SET dismissed = 1 WHERE group_id = ?", (src["group_id"],)
        )
        from . import audit

        audit.append(
            conn, action="artifact.dismiss", resource_type="artifact",
            resource_id=artifact_id, case_id=src["case_id"],
            metadata={"groupId": src["group_id"], "kind": src["kind"],
                      "title": src["title"]},
        )
    return get_artifact(artifact_id)


def dismissed_file_titles(case_id: str) -> set[str]:
    """被用户移除的文件产物名集合（登记链路跳过，防止移除后又被补登记找回）。"""
    db = get_db()
    rows = db.query(
        "SELECT DISTINCT title FROM artifacts WHERE case_id = ? AND kind = 'file' AND dismissed = 1",
        (case_id,),
    )
    return {r["title"] for r in rows}


def get_artifact(artifact_id: str) -> dict[str, Any]:
    """读取单条产物（含解析后的 body；传组内任意版本 id 均返回该版本）。"""
    db = get_db()
    r = db.query_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
    if r is None:
        raise ArtifactError("产物不存在")
    return {
        "id": r["id"], "taskId": r["task_id"], "caseId": r["case_id"],
        "kind": r["kind"], "title": r["title"],
        "body": json.loads(r["body_json"] or "{}"),
        "createdAt": r["created_at"],
        "groupId": r["group_id"], "versionNo": r["version_no"],
    }


# ---------------------------------------------------------------------------
# 文件类产物直接编辑（无工作副本）：编辑 AI 生成文档本体，保存写回工作空间文件
# ---------------------------------------------------------------------------

# 直接编辑支持的文本格式（docx 单独走 Word 模型；其余二进制格式不支持）
_TEXT_EDIT_EXTS = {"md", "markdown", "txt", "html", "htm", "csv"}
_TEXT_PREVIEW_EXTS = {"md", "markdown", "txt", "csv"}
MAX_EDIT_BYTES = 20_000_000


def _editable_target(artifact_id: str) -> tuple[dict[str, Any], Path]:
    """校验产物可编辑并解析工作空间文件路径（文件名必须是纯名称，防穿越）。"""
    from .cases import require_active_case

    art = get_artifact(artifact_id)
    if art["kind"] != "file":
        raise ArtifactError("该产物不是文件类产物，不能直接编辑")
    require_active_case(str(art["caseId"]))
    return art, _file_source(art)


def _file_source(art: dict[str, Any]) -> Path:
    """解析文件类产物的工作空间源文件（纯文件名防穿越，不触碰库文件）。

    不做案件活跃校验：导出是只读操作，归档案件也应能导出产物；
    编辑入口（_editable_target）另行强制可写状态。
    """
    from .sandbox import workspace_root

    body = art.get("body") or {}
    name = str(body.get("fileName") or "")
    if not name or name in (".", "..") or Path(name).name != name or name.startswith("."):
        raise ArtifactError("产物文件名缺失或非法")
    # 工作空间内部文件（数据库/目录标记）不属于交付物，禁止经产物通道改写
    if name in {"lawbuddy.db", "lawbuddy.db-wal", "lawbuddy.db-shm"}:
        raise ArtifactError("产物文件名缺失或非法")
    src = workspace_root() / name
    if not src.is_file():
        raise ArtifactError("产物文件已不在工作空间中")
    return src


def open_editable(artifact_id: str) -> dict[str, Any]:
    """打开 AI 生成文档的直接编辑视图（docx → Word 模型；xlsx/pptx → 单元格/文本框；
    md/html/txt → 源码）。

    原件中的复杂版式按简化规则收敛并返回 warnings，保存时会覆盖原文件，
    因此只允许文件类产物（AI 生成的交付物），不触碰用户导入的案件原件。
    """
    art, src = _editable_target(artifact_id)
    ext = src.suffix.lower().lstrip(".")
    if src.stat().st_size > MAX_EDIT_BYTES:
        raise ArtifactError("文件超过 20MB，无法在线编辑")
    if ext == "docx":
        from .word import from_docx

        data = src.read_bytes()
        model, warnings = from_docx(data, simplify=True)
        return {
            "artifactId": art["id"], "title": art["title"], "kind": "word",
            "model": model, "warnings": warnings,
        }
    if ext in _TEXT_EDIT_EXTS:
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ArtifactError("产物内容不可读") from exc
        return {"artifactId": art["id"], "title": art["title"], "kind": "text", "text": text}
    if ext == "xlsx":
        return _open_sheet_editable(art, src)
    if ext == "pptx":
        return _open_slide_editable(art, src)
    raise ArtifactError(f"该文件格式（{ext.upper()}）暂不支持直接编辑")


_SHEET_EDIT_MAX_ROWS = 500
_SHEET_EDIT_MAX_COLS = 40
_SLIDE_TEXT_MAX = 20_000


def _freeze_offsets(freeze_panes: str | None) -> tuple[int, int]:
    """'B2' → (冻结 1 行, 冻结 1 列)；无冻结返回 (0, 0)。"""
    if not freeze_panes:
        return 0, 0
    letters = "".join(ch for ch in freeze_panes if ch.isalpha())
    digits = "".join(ch for ch in freeze_panes if ch.isdigit())
    if not letters or not digits:
        return 0, 0
    col = 0
    for ch in letters.upper():
        col = col * 26 + (ord(ch) - 64)
    return max(0, int(digits) - 1), max(0, col - 1)


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        try:
            return str(value.isoformat(sep=" "))[:19]
        except TypeError:
            return str(value)
    return str(value)


def _open_sheet_editable(art: dict[str, Any], src: Path) -> dict[str, Any]:
    """读取 XLSX 为可编辑网格模型（只展示前 500 行 × 40 列，超出明确告警）。"""
    from openpyxl import load_workbook

    workbook = load_workbook(str(src), data_only=False)
    sheets: list[dict[str, Any]] = []
    warnings: list[str] = []
    truncated = False
    risky = False
    for sheet in workbook.worksheets:
        max_row = min(sheet.max_row, _SHEET_EDIT_MAX_ROWS)
        max_col = min(sheet.max_column, _SHEET_EDIT_MAX_COLS)
        if sheet.max_row > max_row or sheet.max_column > max_col:
            truncated = True
        rows = [
            [_cell_text(sheet.cell(row, col).value) for col in range(1, max_col + 1)]
            for row in range(1, max_row + 1)
        ]
        merges: list[dict[str, int]] = []
        for merged in sheet.merged_cells.ranges:
            if merged.min_row > max_row or merged.min_col > max_col:
                continue
            merges.append({
                "row": merged.min_row - 1,
                "col": merged.min_col - 1,
                "rowSpan": min(merged.max_row, max_row) - merged.min_row + 1,
                "colSpan": min(merged.max_col, max_col) - merged.min_col + 1,
            })
        freeze_rows, freeze_cols = _freeze_offsets(sheet.freeze_panes)
        if getattr(sheet, "_charts", None) or getattr(sheet, "_images", None):
            risky = True
        sheets.append({
            "name": sheet.title,
            "startRow": 0,
            "startCol": 0,
            "rowCount": max_row,
            "colCount": max_col,
            "rows": rows,
            "merges": merges,
            "freezeRows": freeze_rows,
            "freezeCols": freeze_cols,
        })
    if truncated:
        warnings.append(
            f"仅显示前 {_SHEET_EDIT_MAX_ROWS} 行 × {_SHEET_EDIT_MAX_COLS} 列；"
            "超出范围的内容请在原文件中查看"
        )
    if risky:
        warnings.append("该工作簿含图表/图片，保存后可能丢失（保存前会要求确认）")
    return {
        "artifactId": art["id"], "title": art["title"], "kind": "sheet",
        "sheets": sheets, "warnings": warnings, "risky": risky,
    }


def _open_slide_editable(art: dict[str, Any], src: Path) -> dict[str, Any]:
    """读取 PPTX 每页文本框（只暴露文字，图表/图片/表格保持原样）。"""
    from pptx import Presentation

    presentation = Presentation(str(src))
    slides: list[dict[str, Any]] = []
    warnings: list[str] = []
    has_table = False
    for index, slide in enumerate(presentation.slides):
        boxes: list[dict[str, Any]] = []
        for shape in slide.shapes:
            if getattr(shape, "has_table", False):
                has_table = True
                continue
            if not getattr(shape, "has_text_frame", False):
                continue
            text = str(shape.text_frame.text or "")
            if not text.strip():
                continue
            boxes.append({
                "shapeId": int(shape.shape_id),
                "name": str(shape.name),
                "text": text[:4000],
            })
        title_shape = slide.shapes.title
        title = str(title_shape.text).strip() if title_shape is not None else ""
        slides.append({
            "index": index,
            "title": title or f"第 {index + 1} 页",
            "boxes": boxes,
        })
    if has_table:
        warnings.append("表格内文字暂不支持编辑，保存时表格保持原样")
    return {
        "artifactId": art["id"], "title": art["title"], "kind": "slide",
        "slides": slides, "warnings": warnings,
    }


def _set_text_frame_text(text_frame: Any, text: str) -> None:
    """写入文本框文字：保留首段首个 run 的字体/字号/颜色，其余段落重建。"""
    paragraphs = text_frame.paragraphs
    for extra in list(paragraphs)[1:]:
        extra._p.getparent().remove(extra._p)
    lines = text.split("\n")
    first = text_frame.paragraphs[0]
    runs = list(first.runs)
    if runs:
        runs[0].text = lines[0]
        for run in runs[1:]:
            run._r.getparent().remove(run._r)
    else:
        first.text = lines[0]
    for line in lines[1:]:
        paragraph = text_frame.add_paragraph()
        paragraph.text = line


def _sheet_value(old_value: Any, new_value: str) -> Any:
    """保留数字类型：原单元格是数字且新值合法时写数字；其余按文本写入（保护 00123 等编号）。"""
    if new_value == "":
        return None
    if isinstance(old_value, (int, float)) and not isinstance(old_value, bool):
        if re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", new_value):
            return float(new_value) if "." in new_value else int(new_value)
    return new_value


def create_editable_copy(document_id: str) -> dict[str, Any]:
    """为导入的 Excel/PPT 原件创建可编辑副本（原件只读，副本作为文件产物登记）。

    副本保存在工作空间根目录并登记为 kind=file 产物；后续编辑只改副本，
    原件与原件版本链不受影响。含密码的文档必须先解锁。
    """
    from . import documents, tempstore
    from .cases import require_active_case

    db = get_db()
    row = db.query_one(
        """
        SELECT d.id, d.case_id, d.kind, d.title, d.status, d.active_version_id,
               v.blob_uri, v.unlocked_blob_uri
        FROM documents d LEFT JOIN document_versions v ON v.id = d.active_version_id
        WHERE d.id = ?
        """,
        (document_id,),
    )
    if row is None or row["active_version_id"] is None:
        raise ArtifactError("原件不存在或没有可用版本")
    kind = str(row["kind"] or "").lower()
    if kind not in ("xlsx", "pptx"):
        raise ArtifactError("仅 Excel/PPT 原件支持创建可编辑副本")
    require_active_case(str(row["case_id"]))
    if row["status"] == "need_password" and not row["unlocked_blob_uri"]:
        raise ArtifactError("文档已加密，请先解锁再创建副本")

    root = documents._workspace_root()
    uri = documents._resolve_blob_uri(str(row["active_version_id"]), str(row["blob_uri"]))
    with tempstore.materialized(root, uri, str(row["case_id"]), kind) as source:
        data = source.read_bytes()

    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(str(row["title"])).stem).strip(" .") or "文件"
    base = f"{stem}_工作副本"
    name = f"{base}.{kind}"
    index = 2
    while (root / name).exists():
        name = f"{base}{index}.{kind}"
        index += 1
    target = root / name
    try:
        target.write_bytes(data)
        artifact_id = save_artifact(None, str(row["case_id"]), "file", name, {
            "fileName": name,
            "sizeBytes": len(data),
            "sourceDocumentId": document_id,
            "sourceVersionId": str(row["active_version_id"]),
            "editableCopy": True,
        })
    except Exception:
        target.unlink(missing_ok=True)
        raise

    from . import audit

    with get_db().transaction() as conn:
        audit.append(
            conn, action="document.editable_copy", resource_type="document",
            resource_id=document_id, case_id=str(row["case_id"]),
            metadata={"artifactId": artifact_id, "kind": kind, "sizeBytes": len(data)},
        )
    return {"artifactId": artifact_id, "title": name}


def save_sheet_edits(artifact_id: str, changes: list[dict[str, Any]],
                     create_version: bool = False, confirm_loss: bool = False) -> dict[str, Any]:
    """Excel 单元格增量写回：只改指定单元格，其余单元格与样式保持原样。

    openpyxl 重新保存会丢失图表/图片：含图表/图片时必须显式确认，
    否则拒绝保存，避免用户无感知丢内容。
    """
    from openpyxl import load_workbook

    art, src = _editable_target(artifact_id)
    ext = src.suffix.lower().lstrip(".")
    if ext != "xlsx":
        raise ArtifactError("仅支持编辑 .xlsx 产物")
    if not isinstance(changes, list) or len(changes) > 5000:
        raise ArtifactError("单元格修改数量不合法")
    workbook = load_workbook(str(src))
    risky = any(
        getattr(sheet, "_charts", None) or getattr(sheet, "_images", None)
        for sheet in workbook.worksheets
    )
    if risky and not confirm_loss:
        raise ArtifactError("SHEET_LOSS_CONFIRM：该文件含图表/图片，保存后可能丢失；请确认后重试")

    for change in changes:
        if not isinstance(change, dict):
            raise ArtifactError("单元格修改格式不合法")
        name = str(change.get("sheet") or "")
        if name not in workbook.sheetnames:
            raise ArtifactError(f"工作表不存在：{name}")
        row, col = change.get("row"), change.get("col")
        if not isinstance(row, int) or not isinstance(col, int):
            raise ArtifactError("单元格行列必须为整数")
        if not (0 <= row < 1_048_576 and 0 <= col < 16_384):
            raise ArtifactError("单元格行列超出 Excel 范围")
        value = change.get("value")
        if not isinstance(value, str) or len(value) > 32_767:
            raise ArtifactError("单元格内容不合法或超出长度限制")
        cell = workbook[name].cell(row + 1, col + 1)
        cell.value = _sheet_value(cell.value, value)

    buffer = io.BytesIO()
    workbook.save(buffer)
    warnings = ["该文件含图表/图片，保存后可能丢失"] if risky else []
    result = _commit_editable(artifact_id, art, src, ext, buffer.getvalue(), create_version)
    if warnings:
        result["warnings"] = warnings
    return result


def save_slide_texts(artifact_id: str, edits: list[dict[str, Any]],
                     create_version: bool = False) -> dict[str, Any]:
    """PPTX 文本框轻量编辑：只改指定文本，形状/版式/图表/图片保持原样。"""
    from pptx import Presentation

    art, src = _editable_target(artifact_id)
    ext = src.suffix.lower().lstrip(".")
    if ext != "pptx":
        raise ArtifactError("仅支持编辑 .pptx 产物")
    if not isinstance(edits, list) or len(edits) > 300:
        raise ArtifactError("文本框修改数量不合法")
    presentation = Presentation(str(src))
    slides = list(presentation.slides)
    for edit in edits:
        if not isinstance(edit, dict):
            raise ArtifactError("文本框修改格式不合法")
        index, shape_id = edit.get("slideIndex"), edit.get("shapeId")
        text = edit.get("text")
        if not isinstance(index, int) or not (0 <= index < len(slides)):
            raise ArtifactError("幻灯片序号不合法")
        if not isinstance(shape_id, int) or not isinstance(text, str) or len(text) > _SLIDE_TEXT_MAX:
            raise ArtifactError("文本框内容不合法")
        shape = next(
            (item for item in slides[index].shapes
             if int(getattr(item, "shape_id", -1)) == shape_id
             and getattr(item, "has_text_frame", False)),
            None,
        )
        if shape is None:
            raise ArtifactError("文本框不存在或不可编辑")
        _set_text_frame_text(shape.text_frame, text)

    buffer = io.BytesIO()
    presentation.save(buffer)
    return _commit_editable(artifact_id, art, src, ext, buffer.getvalue(), create_version)


def _atomic_replace(src: Path, data: bytes) -> None:
    """原子替换目标文件（Windows 下被杀毒/文件系统短暂占用时小退避重试）。"""
    import os
    import time

    tmp = src.with_name(f".{src.name}.{uuid.uuid4().hex}.tmp")
    written = False
    try:
        tmp.write_bytes(data)
        written = True
        for attempt in range(6):
            try:
                os.replace(tmp, src)
                break
            except OSError:
                if attempt == 5:
                    raise
                time.sleep(0.25)
    except OSError as exc:
        if written:
            tmp.unlink(missing_ok=True)
        raise ArtifactError("保存失败：文件可能被占用或工作空间不可写") from exc


def _commit_editable(artifact_id: str, art: dict[str, Any], src: Path, ext: str,
                     data: bytes, create_version: bool,
                     preview_text: str | None = None) -> dict[str, Any]:
    """写入编辑结果、更新概览、写审计并按需追加版本（不发路径给前端）。"""
    _atomic_replace(src, data)
    body = dict(art.get("body") or {})
    body["sizeBytes"] = len(data)
    if isinstance(preview_text, str) and ext in _TEXT_PREVIEW_EXTS:
        body["intro"] = preview_text[:2000]
    with get_db().transaction() as conn:
        conn.execute(
            "UPDATE artifacts SET body_json = ? WHERE id = ?",
            (json.dumps(body, ensure_ascii=False), artifact_id),
        )
        from . import audit

        audit.append(
            conn, action="artifact.edit", resource_type="artifact",
            resource_id=artifact_id, case_id=art["caseId"],
            metadata={"format": ext, "sizeBytes": len(data),
                      "createVersion": bool(create_version)},
        )
    version_no = None
    if create_version:
        saved = save_artifact_version(artifact_id, body)
        version_no = saved.get("versionNo")
    return {"ok": True, "sizeBytes": len(data), "versionNo": version_no}


def save_editable(
    artifact_id: str,
    model: dict[str, Any] | None = None,
    text: str | None = None,
    create_version: bool = False,
) -> dict[str, Any]:
    """把编辑结果直接写回原文档文件（AI 交付物），不做工作副本。

    - docx：校验 Word 模型后序列化为 DOCX，原子替换工作空间文件；
    - 文本格式：UTF-8 写回，并同步产物预览摘要；
    - create_version=True 时在版本组内追加一个编辑版本（历史版本保留记录），
      自动保存只覆盖当前文件，避免版本泛滥；
    - 审计只记格式、字节数与是否建版本，不写正文。
    """
    from .word import to_docx, validate_model

    art, src = _editable_target(artifact_id)
    ext = src.suffix.lower().lstrip(".")
    if ext == "docx":
        if not isinstance(model, dict):
            raise ArtifactError("缺少 Word 文档模型")
        validate_model(model)
        data = to_docx(model)
    elif ext in _TEXT_EDIT_EXTS:
        if not isinstance(text, str):
            raise ArtifactError("缺少文本内容")
        data = text.encode("utf-8")
        if len(data) > MAX_EDIT_BYTES:
            raise ArtifactError("文件超过 20MB，无法保存")
    else:
        raise ArtifactError(f"该文件格式（{ext.upper()}）暂不支持直接编辑")

    return _commit_editable(
        artifact_id, art, src, ext, data, create_version,
        preview_text=text if isinstance(text, str) else None,
    )



# 同格式导出的源扩展名（原文件字节复制优先：同格式导出不经重建，100% 保真原版式）
_SAME_FMT_EXTS = {
    "markdown": {"md", "markdown"},
    "docx": {"docx"},
    "pdf": {"pdf"},
    "xlsx": {"xlsx", "xlsm"},
    "pptx": {"pptx"},
    "html": {"html", "htm"},
    "txt": {"txt", "log"},
    "csv": {"csv"},
    "json": {"json"},
    "svg": {"svg"},
    "xml": {"xml"},
    "yaml": {"yaml", "yml"},
}




def _source_ext(art: dict[str, Any]) -> str:
    """文件类产物的源扩展名（小写，不含点）。"""
    try:
        return _file_source(art).suffix.lower().lstrip(".")
    except ArtifactError:
        return ""


def export_artifact(artifact_id: str, fmt: str, dest_path: str) -> dict[str, Any]:
    """导出产物到目标路径（markdown / docx / pdf / xlsx / pptx / html / txt / csv / json），写审计（不含 destPath）。

    保真策略（原文件字节复制优先，导出产物必须是真实原生文件）：
    - 文件类产物同格式导出：直接复制工作空间源文件字节，不经任何重建
      （AI 编辑结果已写回源文件，复制即当前稿）；
    - 文件类产物跨格式导出：从源文件真实内容提取块序列再经原生库渲染，
      不使用 intro 摘要占位（避免导出空壳假文件）；
    - 结构化产物（报告 / 证据卡 / 策略等）：body → 块序列 → 原生渲染器。
    """
    art = get_artifact(artifact_id)
    if fmt not in FORMATS and not (art["kind"] == "file" and fmt == _source_ext(art)):
        raise ArtifactError(f"不支持的导出格式：{fmt}")
    try:
        if art["kind"] == "file":
            src = _file_source(art)
            ext = src.suffix.lower().lstrip(".")
            if fmt == ext or ext in _SAME_FMT_EXTS.get(fmt, ()):
                import shutil

                shutil.copyfile(src, dest_path)
            else:
                blocks = _file_to_blocks(src, ext, art["title"])
                _write_blocks(blocks, fmt, dest_path, art["title"])
        elif fmt == "json":
            Path(dest_path).write_text(
                json.dumps(art["body"], ensure_ascii=False, indent=2), encoding="utf-8"
            )
        elif fmt == "csv":
            Path(dest_path).write_text(
                _render_csv(_body_to_blocks(art["title"], art["body"])), encoding="utf-8"
            )
        elif fmt == "pptx" and (art["body"] or {}).get("viz"):
            # 可视化产物用原生形状/连接线绘制，而不是把图退化成表格
            from . import render as _render

            _render.viz_to_pptx(art["body"]["viz"], dest_path, art["title"])
        else:
            _write_blocks(_body_to_blocks(art["title"], art["body"]), fmt, dest_path, art["title"])
    except OSError as exc:
        raise ArtifactError(f"导出失败：无法写入目标文件（{type(exc).__name__}）") from exc

    size = Path(dest_path).stat().st_size
    from . import audit

    db = get_db()
    with db.transaction() as conn:
        audit.append(
            conn, action="artifact.export", resource_type="artifact",
            resource_id=artifact_id, case_id=art["caseId"],
            metadata={
                "format": fmt, "sizeBytes": size,
                "sourceVersionNo": art.get("versionNo"),
            },
        )
    return {
        "ok": True, "fileName": Path(dest_path).name, "sizeBytes": size,
        "sourceVersionNo": art.get("versionNo"),
    }


# ---------------------------------------------------------------------------
# 产物批注：AI 生成产物（报告/图表/文件等）的派生数据，产物本体不变。
# 产物无页模型（整篇单锚），批注绑定具体产物版本 id，版本更新后旧批注不跨
# 版本显示；quote 供前端在正文定位荧光高亮（定位由读取端计算，不落库）。
# ---------------------------------------------------------------------------

_ANNOT_COLS = (
    "id, artifact_id, quote, body, color, status, resolved_at, created_at, updated_at"
)


def _artifact_annotation_to_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "artifactId": r["artifact_id"],
        "quote": r["quote"],
        # 产物整篇渲染、无页文本定位语义，quote 由前端正文匹配；恒可定位
        "quoteMissing": False,
        "body": r["body"],
        "color": r["color"],
        "status": r["status"],
        "resolvedAt": r["resolved_at"],
        "createdAt": r["created_at"],
        "updatedAt": r["updated_at"],
    }


def _get_artifact_annotation(annotation_id: str) -> dict[str, Any] | None:
    db = get_db()
    r = db.query_one(
        f"SELECT {_ANNOT_COLS} FROM artifact_annotations WHERE id = ?", (annotation_id,)
    )
    return _artifact_annotation_to_row(r) if r is not None else None


def list_artifact_annotations(artifact_id: str) -> list[dict[str, Any]]:
    """产物全部批注（只返回该产物版本的批注；传组内其他版本 id 不跨版本显示）。"""
    db = get_db()
    art = db.query_one("SELECT id FROM artifacts WHERE id = ?", (artifact_id,))
    if art is None:
        raise ArtifactError("产物不存在")
    rows = db.query(
        f"SELECT {_ANNOT_COLS} FROM artifact_annotations "
        "WHERE artifact_id = ? ORDER BY created_at",
        (artifact_id,),
    )
    return [_artifact_annotation_to_row(r) for r in rows]


def add_artifact_annotation(
    artifact_id: str, body: str, quote: str = "", color: str = ""
) -> dict[str, Any]:
    """新增产物批注：派生数据，产物本体不变；审计只记长度与标记，不落正文。"""
    from .cases import require_active_case
    from .documents import _norm_annotation_color

    db = get_db()
    body = (body or "").strip()
    if not body:
        raise ArtifactError("批注内容不能为空")
    quote = (quote or "").strip() or None
    color_val = _norm_annotation_color((color or "").strip() or None)
    art = db.query_one("SELECT id, case_id FROM artifacts WHERE id = ?", (artifact_id,))
    if art is None:
        raise ArtifactError("产物不存在")
    require_active_case(art["case_id"])

    ann_id = str(uuid.uuid4())
    ts = now_iso()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO artifact_annotations(id, artifact_id, case_id, quote, body, "
            "color, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (ann_id, artifact_id, art["case_id"], quote, body, color_val, ts, ts),
        )
        from . import audit

        audit.append(
            conn, action="artifact.annotate_add", resource_type="artifact_annotation",
            resource_id=ann_id, case_id=art["case_id"],
            metadata={"artifactId": artifact_id, "bodyLen": len(body),
                      "hasQuote": quote is not None},
        )
    return _get_artifact_annotation(ann_id)


def update_artifact_annotation(annotation_id: str, body: str, color: str = "") -> dict[str, Any]:
    """编辑批注内容与荧光色：body/color 与 updated_at 更新，锚定与状态不变。"""
    from .cases import require_active_case
    from .documents import _norm_annotation_color

    db = get_db()
    body = (body or "").strip()
    if not body:
        raise ArtifactError("批注内容不能为空")
    row = db.query_one(
        "SELECT artifact_id, case_id, body FROM artifact_annotations WHERE id = ?",
        (annotation_id,),
    )
    if row is None:
        raise ArtifactError("批注不存在")
    require_active_case(row["case_id"])
    with db.transaction() as conn:
        if color:
            conn.execute(
                "UPDATE artifact_annotations SET body = ?, color = ?, updated_at = ? WHERE id = ?",
                (body, _norm_annotation_color((color or "").strip() or None),
                 now_iso(), annotation_id),
            )
        else:
            conn.execute(
                "UPDATE artifact_annotations SET body = ?, updated_at = ? WHERE id = ?",
                (body, now_iso(), annotation_id),
            )
        from . import audit

        audit.append(
            conn, action="artifact.annotate_update", resource_type="artifact_annotation",
            resource_id=annotation_id, case_id=row["case_id"],
            metadata={"artifactId": row["artifact_id"],
                      "oldLen": len(row["body"]), "newLen": len(body)},
        )
    return _get_artifact_annotation(annotation_id)


def set_artifact_annotation_resolved(annotation_id: str, resolved: bool) -> dict[str, Any]:
    """批注状态流转 open ↔ resolved（核实流转语义与文档批注一致）。"""
    from .cases import require_active_case

    db = get_db()
    row = db.query_one(
        "SELECT artifact_id, case_id, status FROM artifact_annotations WHERE id = ?",
        (annotation_id,),
    )
    if row is None:
        raise ArtifactError("批注不存在")
    require_active_case(row["case_id"])
    new_status = "resolved" if resolved else "open"
    with db.transaction() as conn:
        conn.execute(
            "UPDATE artifact_annotations SET status = ?, resolved_at = ?, updated_at = ? "
            "WHERE id = ?",
            (new_status, now_iso() if resolved else None, now_iso(), annotation_id),
        )
        from . import audit

        audit.append(
            conn, action="artifact.annotate_resolve", resource_type="artifact_annotation",
            resource_id=annotation_id, case_id=row["case_id"],
            metadata={"artifactId": row["artifact_id"],
                      "from": row["status"], "to": new_status},
        )
    return _get_artifact_annotation(annotation_id)


def delete_artifact_annotation(annotation_id: str) -> dict[str, Any]:
    """删除批注（用户派生数据可删）；审计记录长度标记，不落正文。"""
    from .cases import require_active_case

    db = get_db()
    row = db.query_one(
        "SELECT artifact_id, case_id, body, quote FROM artifact_annotations WHERE id = ?",
        (annotation_id,),
    )
    if row is None:
        raise ArtifactError("批注不存在")
    require_active_case(row["case_id"])
    with db.transaction() as conn:
        conn.execute("DELETE FROM artifact_annotations WHERE id = ?", (annotation_id,))
        from . import audit

        audit.append(
            conn, action="artifact.annotate_delete", resource_type="artifact_annotation",
            resource_id=annotation_id, case_id=row["case_id"],
            metadata={"artifactId": row["artifact_id"],
                      "bodyLen": len(row["body"]), "hasQuote": bool(row["quote"])},
        )
    return {"deleted": True, "annotationId": annotation_id}


# ---------------------------------------------------------------------------
# body → 块序列（Markdown 与 DOCX 共用；块：h1/h2/p/li/table）
# ---------------------------------------------------------------------------

def _append_cite(blocks: list[tuple[str, Any]], item: dict[str, Any], key: str = "cite") -> None:
    """引证单独成块（`cite`），供 DOCX 导出为原生脚注；其余文本格式按来源后缀呈现。"""
    cite = item.get(key)
    if isinstance(cite, dict) and str(cite.get("label") or "").strip():
        blocks.append(("cite", cite))


def _body_to_blocks(title: str, body: dict[str, Any]) -> list[tuple[str, Any]]:
    blocks: list[tuple[str, Any]] = [("h1", title)]

    report = body.get("report")
    viz = body.get("viz")
    evidence = body.get("evidence")
    strategy = body.get("strategy")

    if report:
        if report.get("overview"):
            blocks.append(("p", str(report["overview"])))
        sections = (
            ("parties", "当事人", lambda it: f"{it.get('name', '')}（{it.get('role', '')}）：{it.get('note', '')}"),
            ("timeline", "时间线", lambda it: f"{it.get('date', '')} {it.get('event', '')}"),
            ("disputes", "争议焦点", lambda it: f"{it.get('title', '')}：{it.get('desc', '')}"),
            ("facts", "事实", lambda it: f"[{it.get('stance', '')}] {it.get('text', '')}"),
            ("contradictions", "矛盾证据", lambda it: f"{it.get('topic', '')}：{it.get('sideA', '')} vs {it.get('sideB', '')}"
                + (f"（{it.get('note')}）" if it.get("note") else "")),
            ("legalBasis", "法律依据", lambda it: f"{it.get('title', '')}：{it.get('point', '')}"),
        )
        for key, label, fmt in sections:
            items = [it for it in (report.get(key) or []) if isinstance(it, dict)]
            if not items:
                continue
            blocks.append(("h2", label))
            for it in items:
                text = fmt(it).strip()
                if text:
                    blocks.append(("li", text))
                    if key == "contradictions":
                        # 矛盾双侧引证都要落到来源里（R-004），不能只留一侧
                        _append_cite(blocks, it, "citeA")
                        _append_cite(blocks, it, "citeB")
                    else:
                        _append_cite(blocks, it)
        for key, label in (("missing", "缺失材料"), ("nextSteps", "下一步工作")):
            items = [str(x) for x in (report.get(key) or []) if str(x).strip()]
            if items:
                blocks.append(("h2", label))
                for it in items:
                    blocks.append(("li", it))
    elif viz:
        tl = (viz.get("timeline") or {}).get("nodes") or []
        if tl:
            blocks.append(("h2", "时间线"))
            blocks.append(("table", {"header": ["日期", "事件", "参与者"],
                                     "rows": [[str(n.get("date", "")), str(n.get("event", "")),
                                               "、".join(str(a) for a in (n.get("actors") or []))] for n in tl]}))
        g = viz.get("relationGraph") or {}
        nodes = g.get("nodes") or []
        if nodes:
            blocks.append(("h2", "关系图（节点）"))
            blocks.append(("table", {"header": ["名称", "类型", "角色"],
                                     "rows": [[str(n.get("name", "")), str(n.get("kind", "")), str(n.get("role", ""))] for n in nodes]}))
        edges = g.get("edges") or []
        if edges:
            blocks.append(("h2", "关系图（关系）"))
            for e in edges:
                blocks.append(("li", f"{e.get('from', '')} → {e.get('to', '')}：{e.get('label', '')}"))
                _append_cite(blocks, e)
        for f in viz.get("flows") or []:
            f_nodes = f.get("nodes") or []
            if not f_nodes:
                continue
            blocks.append(("h2", f"流程图：{f.get('title', '')}（节点）"))
            blocks.append(("table", {"header": ["id", "节点", "形状"],
                                     "rows": [[str(n.get("id", "")), str(n.get("label", "")), str(n.get("shape") or "rect")] for n in f_nodes]}))
            f_edges = f.get("edges") or []
            if f_edges:
                blocks.append(("h2", f"流程图：{f.get('title', '')}（流转）"))
                for e in f_edges:
                    label = str(e.get("label") or "")
                    dashed = "（待核实，虚线）" if e.get("dashed") else ""
                    blocks.append(("li", f"{e.get('from', '')} → {e.get('to', '')}" + (f"：{label}" if label else "") + dashed))
                    _append_cite(blocks, e)
    elif evidence:
        for card in evidence.get("evidence") or []:
            blocks.append(("h2", f"证据：{card.get('name', '')}"))
            for label, key in (("类型", "type"), ("形成时间", "formedAt"), ("提供方", "provider"),
                               ("保管情况", "custody"), ("证明目的", "purpose"), ("关键片段", "keyFragment")):
                if str(card.get(key) or "").strip():
                    blocks.append(("p", f"{label}：{card[key]}"))
            for label, key in (("真实性", "authenticity"), ("合法性", "legality"),
                               ("关联性", "relevance"), ("证明力", "weight")):
                if str(card.get(key) or "").strip():
                    blocks.append(("p", f"【分析意见】{label}：{card[key]}"))
            if str(card.get("reinforce") or "").strip():
                blocks.append(("p", f"补强建议：{card['reinforce']}"))
        matrix = evidence.get("matrix") or {}
        cells = matrix.get("cells") or []
        if cells:
            blocks.append(("h2", "争点证据矩阵"))
            blocks.append(("table", {"header": ["争议焦点", "证据", "关系", "备注"],
                                     "rows": [[str(c.get("dispute", "")), str(c.get("evidence", "")),
                                               str(c.get("relation", "")), str(c.get("note", ""))] for c in cells]}))
    elif strategy:
        if strategy.get("position"):
            blocks.append(("p", f"委托立场：{strategy['position']}"))
        for key, label in (("keyDisputes", "关键争点"), ("favorable", "有利条件"), ("unfavorable", "不利条件")):
            items = strategy.get(key) or []
            texts = [it.get("text", "") if isinstance(it, dict) else str(it) for it in items]
            texts = [t for t in texts if str(t).strip()]
            if texts:
                blocks.append(("h2", label))
                for t in texts:
                    blocks.append(("li", str(t)))
        for plan in strategy.get("plans") or []:
            blocks.append(("h2", f"方案：{plan.get('title', '')}"))
            if plan.get("summary"):
                blocks.append(("p", str(plan["summary"])))
            for it in plan.get("steps") or []:
                if str(it).strip():
                    blocks.append(("li", str(it)))
            if plan.get("risk"):
                blocks.append(("p", f"风险：{plan['risk']}"))
    else:
        # 精简要点 / 文件登记 / 对话分析结构
        if body.get("intro"):
            blocks.append(("p", str(body["intro"])))
        if body.get("answer"):
            # chat 产物（自主循环收尾）：导出全文 + 引证说明
            blocks.append(("p", str(body["answer"])))
        for p in body.get("points") or []:
            blocks.append(("li", f"{p.get('title', '')} —— {p.get('desc', '')}"))

    if body.get("degradedReason"):
        blocks.append(("p", "注：本产物为离线确定性分析结果（未调用云端模型），关键结论请以原文核实。"))
    if body.get("audit"):
        blocks.append(("p", f"说明：{body['audit']}"))
    return blocks


def _cite_text(cite: Any) -> str:
    """引证块 → 文本来源后缀（非 DOCX 格式沿用内联来源，不丢出处）。"""
    if isinstance(cite, dict):
        label = str(cite.get("label") or "").strip()
        if label:
            return f"（来源：{label}）"
    return ""


def _render_markdown(blocks: list[tuple[str, Any]]) -> str:
    lines: list[str] = []
    last_index: int | None = None
    for kind, val in blocks:
        if kind == "h1":
            lines += [f"# {val}", ""]
            last_index = None
        elif kind == "h2":
            lines += [f"## {val}", ""]
            last_index = None
        elif kind == "p":
            lines += [str(val), ""]
            last_index = len(lines) - 2
        elif kind == "li":
            lines.append(f"- {val}")
            last_index = len(lines) - 1
        elif kind == "cite":
            if last_index is not None:
                lines[last_index] += _cite_text(val)
        elif kind == "table":
            header = val["header"]
            lines += ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
            for row in val["rows"]:
                lines.append("| " + " | ".join(str(c) for c in row) + " |")
            lines.append("")
            last_index = None
    return "\n".join(lines).rstrip() + "\n"


def _render_text(blocks: list[tuple[str, Any]]) -> str:
    """块序列 → 纯文本（标题带下划线，表格按制表符对齐）。"""
    lines: list[str] = []
    last_index: int | None = None
    for kind, val in blocks:
        if kind == "h1":
            lines += [str(val), "=" * max(2, len(str(val))), ""]
            last_index = None
        elif kind == "h2":
            lines += [str(val), "-" * max(2, len(str(val))), ""]
            last_index = None
        elif kind == "p":
            lines += [str(val), ""]
            last_index = len(lines) - 2
        elif kind == "li":
            lines.append(f"· {val}")
            last_index = len(lines) - 1
        elif kind == "cite":
            if last_index is not None:
                lines[last_index] += _cite_text(val)
        elif kind == "table":
            header = [str(c) for c in val["header"]]
            lines.append("\t".join(header))
            for row in val["rows"]:
                lines.append("\t".join(str(c) for c in row))
            lines.append("")
            last_index = None
    return "\n".join(lines).rstrip() + "\n"


def _render_csv(blocks: list[tuple[str, Any]]) -> str:
    """块序列 → CSV（只导出表格；无表格数据时明确报错，不生成伪表格）。"""
    import csv
    import io

    tables = [val for kind, val in blocks if kind == "table"]
    if not tables:
        raise ArtifactError("该产物没有表格数据，无法导出 CSV")
    buf = io.StringIO()
    writer = csv.writer(buf)
    for idx, table in enumerate(tables):
        if idx > 0:
            writer.writerow([])
        writer.writerow([str(c) for c in table["header"]])
        for row in table["rows"]:
            writer.writerow([str(c) for c in row])
    return buf.getvalue()


def _render_html(blocks: list[tuple[str, Any]], title: str) -> str:
    """块序列 → 独立 HTML 文档（真实 .html 文件，非 HTML 包壳的 Office 文件）。"""
    from html import escape

    body: list[str] = []
    in_list = False
    for kind, val in blocks:
        if kind != "li" and in_list:
            body.append("</ul>")
            in_list = False
        if kind == "h1":
            body.append(f"<h1>{escape(str(val))}</h1>")
        elif kind == "h2":
            body.append(f"<h2>{escape(str(val))}</h2>")
        elif kind == "p":
            body.append(f"<p>{escape(str(val))}</p>")
        elif kind == "li":
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{escape(str(val))}</li>")
        elif kind == "cite":
            label = str(val.get("label") or "").strip() if isinstance(val, dict) else ""
            if label:
                body.append(f'<div class="src">来源：{escape(label)}</div>')
        elif kind == "table":
            rows = ["<tr>" + "".join(f"<th>{escape(str(c))}</th>" for c in val["header"]) + "</tr>"]
            rows += [
                "<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in row) + "</tr>"
                for row in val["rows"]
            ]
            body.append("<table>" + "".join(rows) + "</table>")
    if in_list:
        body.append("</ul>")
    return (
        '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>{escape(title)}</title><style>"
        "body{font-family:'Microsoft YaHei',SimSun,serif;line-height:1.8;margin:40px auto;max-width:860px;color:#1f2328}"
        "h1{font-size:24px;border-bottom:1px solid #d0d7de;padding-bottom:8px}"
        "h2{font-size:19px;margin-top:28px}"
        "table{border-collapse:collapse;width:100%;margin:14px 0}"
        "th,td{border:1px solid #c9ced6;padding:6px 10px;text-align:left;vertical-align:top}"
        "th{background:#f3f5f8}li{margin:4px 0}"
        ".src{color:#5b6b84;font-size:13px;margin:-2px 0 8px 18px}"
        "</style></head><body><article>"
        + "\n".join(body)
        + "</article></body></html>\n"
    )


def _write_blocks(blocks: list[tuple[str, Any]], fmt: str, dest_path: str, title: str = "") -> None:
    """块序列 → 目标格式文件（markdown / html / txt / docx / pdf / xlsx / pptx，均为原生格式）。"""
    if fmt == "markdown":
        Path(dest_path).write_text(_render_markdown(blocks), encoding="utf-8")
        return
    if fmt == "txt":
        Path(dest_path).write_text(_render_text(blocks), encoding="utf-8")
        return
    if fmt == "html":
        Path(dest_path).write_text(_render_html(blocks, title), encoding="utf-8")
        return
    render = _RENDER_BY_FMT.get(fmt)
    if render is None:
        raise ArtifactError(f"块序列不支持转换为 {fmt}（未提供原生渲染器）")
    from . import render as _render

    getattr(_render, render)(blocks, dest_path)


# ---------------------------------------------------------------------------
# 文件类产物跨格式导出：从源文件真实内容提取块序列（保真重建，不用 intro 占位）
# ---------------------------------------------------------------------------


def _node_text(node: dict[str, Any]) -> str:
    """Word 模型节点的纯文本（递归拼接，hardBreak 视为空白）。"""
    if node.get("type") == "text":
        return str(node.get("text") or "")
    if node.get("type") == "hardBreak":
        return " "
    return "".join(_node_text(child) for child in node.get("content", []))


def _word_model_to_blocks(nodes: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    """Word 模型节点树 → 块序列（heading/paragraph/list/table，嵌套列表展平）。"""
    blocks: list[tuple[str, Any]] = []
    for node in nodes:
        kind = node.get("type")
        if kind == "heading":
            text = _node_text(node).strip()
            if text:
                level = (node.get("attrs") or {}).get("level", 1)
                blocks.append(("h1" if level == 1 else "h2", text))
        elif kind == "paragraph":
            text = _node_text(node).strip()
            if text:
                blocks.append(("p", text))
        elif kind == "listItem":
            for sub_kind, sub_val in _word_model_to_blocks(node.get("content", [])):
                blocks.append(("li", sub_val) if sub_kind in ("p", "li") else (sub_kind, sub_val))
        elif kind in ("bulletList", "orderedList"):
            for child in node.get("content", []):
                blocks.extend(_word_model_to_blocks([child]))
        elif kind == "table":
            rows = [
                [
                    " ".join(t for t in (_node_text(p).strip() for p in cell.get("content", [])) if t)
                    for cell in row.get("content", [])
                ]
                for row in node.get("content", [])
            ]
            rows = [r for r in rows if any(c.strip() for c in r)]
            if rows:
                blocks.append(("table", {"header": rows[0], "rows": rows[1:]}))
    return blocks


def _xlsx_to_blocks(src: Path, title: str) -> list[tuple[str, Any]]:
    """XLSX/XLSM → 块序列：每工作表一节，行数据转原生表格（只取缓存值，不执行公式）。"""
    from openpyxl import load_workbook

    wb = load_workbook(src, read_only=True, data_only=True)
    blocks: list[tuple[str, Any]] = [("h1", title)]
    try:
        for ws in wb.worksheets:
            blocks.append(("h2", str(ws.title or "工作表")))
            rows = [
                ["" if v is None else str(v) for v in row]
                for row in ws.iter_rows(values_only=True)
            ]
            rows = [r for r in rows if any(cell.strip() for cell in r)]
            if not rows:
                blocks.append(("p", "（空工作表）"))
                continue
            blocks.append(("table", {"header": rows[0], "rows": rows[1:]}))
    finally:
        wb.close()
    return blocks


def _pptx_to_blocks(src: Path, title: str) -> list[tuple[str, Any]]:
    """PPTX → 块序列：每张幻灯片一节，文本段落与原生表格逐页保留。"""
    from pptx import Presentation

    prs = Presentation(str(src))
    blocks: list[tuple[str, Any]] = [("h1", title)]
    for i, slide in enumerate(prs.slides, 1):
        blocks.append(("h2", f"幻灯片 {i}"))
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = para.text.strip()
                    if text:
                        blocks.append(("p", text))
            if getattr(shape, "has_table", False):
                rows = [[cell.text.strip() for cell in row.cells] for row in shape.table.rows]
                rows = [r for r in rows if any(c.strip() for c in r)]
                if rows:
                    blocks.append(("table", {"header": rows[0], "rows": rows[1:]}))
    return blocks


def _pdf_to_blocks(src: Path, title: str) -> list[tuple[str, Any]]:
    """PDF → 块序列：逐页提取文本层（扫描件无文本层时显式标注，不静默丢页）。"""
    from pypdf import PdfReader

    reader = PdfReader(str(src))
    blocks: list[tuple[str, Any]] = [("h1", title)]
    for i, page in enumerate(reader.pages, 1):
        blocks.append(("h2", f"第 {i} 页"))
        text = (page.extract_text() or "").strip()
        if not text:
            blocks.append(("p", "（本页无可提取文本，可能为扫描件或纯图片页）"))
            continue
        for para in re.split(r"\n\s*\n", text):
            joined = " ".join(line.strip() for line in para.splitlines() if line.strip())
            if joined:
                blocks.append(("p", joined))
    return blocks


class _HTMLTextBlocks:
    """HTML 可见文本块提取（跨格式导出用）：忽略 script/style，按块级元素成段。"""

    _BLOCK_TAGS = {
        "p", "div", "section", "article", "li", "blockquote", "pre", "tr",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def __init__(self) -> None:
        from html.parser import HTMLParser

        class _Parser(HTMLParser):
            def __init__(self, owner: "_HTMLTextBlocks") -> None:
                super().__init__(convert_charrefs=True)
                self._owner = owner

            def handle_starttag(self, tag: str, attrs: list) -> None:  # noqa: ANN001
                self._owner._on_start(tag)

            def handle_endtag(self, tag: str) -> None:
                self._owner._on_end(tag)

            def handle_data(self, data: str) -> None:
                self._owner._on_data(data)

        self._blocks: list[tuple[str, str]] = []
        self._buf: list[str] = []
        self._skip = 0
        self.parser = _Parser(self)

    def _flush(self, tag: str | None) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if not text:
            return
        kind = "p"
        if tag == "li":
            kind = "li"
        elif tag in ("h1", "h2"):
            kind = "h1"
        elif tag in ("h3", "h4", "h5", "h6"):
            kind = "h2"
        self._blocks.append((kind, text))

    def _on_start(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif not self._skip and tag in self._BLOCK_TAGS:
            self._flush(None)  # 块级起始即分段，段前残留文本单独成段

    def _on_end(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif not self._skip and tag in self._BLOCK_TAGS:
            self._flush(tag)

    def _on_data(self, data: str) -> None:
        if not self._skip:
            self._buf.append(data)

    def blocks(self) -> list[tuple[str, Any]]:
        self._flush(None)
        return list(self._blocks)


def _text_to_blocks(text: str) -> list[tuple[str, Any]]:
    """纯文本 → 块序列（空行分段）。"""
    blocks: list[tuple[str, Any]] = []
    for para in re.split(r"\n\s*\n", text):
        joined = "\n".join(line.rstrip() for line in para.splitlines()).strip()
        if joined:
            blocks.append(("p", joined))
    return blocks


def _read_text_file(src: Path) -> str:
    """读取文本文件：UTF-8 → GB18030 依次尝试，最后才做替换解码。"""
    raw = src.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# 可跨格式转换的源扩展名（其余按原格式导出，不做文本化转换以免导出乱码伪文件）
_CONVERTIBLE_EXTS = {
    "docx", "xlsx", "xlsm", "pptx", "pdf",
    "md", "markdown", "txt", "html", "htm",
    "csv", "tsv", "json", "xml", "yaml", "yml", "code",
}


def _file_to_blocks(src: Path, ext: str, title: str) -> list[tuple[str, Any]]:
    """文件类产物跨格式导出：源文件真实内容 → 块序列（保留结构，不丢内容）。"""
    if ext not in _CONVERTIBLE_EXTS:
        raise ArtifactError(f".{ext} 文件暂不支持转换为其他格式，请使用原格式导出")
    if ext == "docx":
        from .word import from_docx

        model, _warnings = from_docx(src.read_bytes(), simplify=True)
        return [("h1", title), *_word_model_to_blocks(model.get("content", []))]
    if ext in ("xlsx", "xlsm"):
        return _xlsx_to_blocks(src, title)
    if ext == "pptx":
        return _pptx_to_blocks(src, title)
    if ext == "pdf":
        return _pdf_to_blocks(src, title)
    if ext in ("md", "markdown"):
        from .render import md_to_blocks

        return md_to_blocks(_read_text_file(src), title=title)
    text = _read_text_file(src)
    if ext in ("html", "htm"):
        extractor = _HTMLTextBlocks()
        extractor.parser.feed(text)
        extractor.parser.close()
        return [("h1", title), *extractor.blocks()]
    return [("h1", title), *_text_to_blocks(text)]
