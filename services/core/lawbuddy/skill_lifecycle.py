"""技能暂存、扫描与版本管理（LB-HERMES-2026-01 WP09）。

把 Hermes 式「技能自我改进」转成可审计、可回滚的生命周期：

    staging（隔离暂存）→ scan（确定性扫描）→ approve（人工批准）→ version（新版本）
                                              ↘ block（危险项不可强制放行）

硬约束：

1. **暂存优先**：任何安装与自动修改都先写 `skill_staging`，不得直接改 `skills`。
2. **危险项不可强制绕过**：扫描命中提示注入、凭据、危险指令、HTML/XML 外链等
   即 `blocked`，无 force 参数可放行（接口层面不提供）。
3. **已启用技能不被后台覆盖**：批准时新增 `skill_version` 并置为 active，
   旧版本保留可 diff/回滚；`skills.prompt_md` 同步为当前激活版本。
4. **同名不同内容不静默覆盖**：与当前激活版本内容不同时必须走暂存审批，
   相同内容视为幂等。
5. **技能只允许 Markdown/frontmatter 提示词包**：脚本与可执行扩展一律拒绝。
6. **日志不保存敏感正文**：扫描结果只记录命中规则与位置区间，不落原文。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from .storage import get_profile_db
from .workspace import now_iso

MAX_SKILL_BYTES = 256 * 1024
STAGING = "pending"
BLOCKED = "blocked"
APPROVED = "approved"
REJECTED = "rejected"


class SkillLifecycleError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 确定性扫描（只返回规则与位置，不返回原文）
# ---------------------------------------------------------------------------

# 提示注入与控制覆盖
_INJECTION_PATTERNS = (
    ("ignore_instructions", re.compile(r"(忽略|无视|forget|ignore)\s*(以上|之前|前面|all|previous|above)[^\n]{0,20}(指令|规则|instructions?|rules?)", re.I)),
    ("role_override", re.compile(r"(你现在是|you are now|act as)\s*(系统|developer|system|root|admin)", re.I)),
    ("system_prompt_leak", re.compile(r"(输出|print|reveal|show)[^\n]{0,20}(system prompt|系统提示词|初始提示)", re.I)),
    ("tool_escalation", re.compile(r"(无条件|always)\s*(允许|approve|grant)[^\n]{0,20}(bash|shell|写文件|删除|delete)", re.I)),
)
# 凭据与密钥形态
_SECRET_PATTERNS = (
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{16,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}")),
    ("generic_secret", re.compile(r"(?i)(api[_\-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9._\-]{12,}")),
)
# 可执行扩展与危险指令
_EXECUTABLE_PATTERNS = (
    ("script_tag", re.compile(r"<\s*script[^>]*>", re.I)),
    ("shell_block", re.compile(r"^\s*\$\s+\w+|^\s*>\s*(curl|wget|powershell|bash|cmd)\b", re.I | re.M)),
    ("dangerous_command", re.compile(r"(?i)\b(rm\s+-rf|del\s+/[sfq]|format\s+[a-z]:|shutdown|reg\s+delete)\b")),
    ("exec_import", re.compile(r"(?i)\b(os\.system|subprocess\.(popen|run|call)|eval\(|exec\()")),
)
_HTML_PATTERN = re.compile(r"<\s*(html|iframe|object|embed|form|link|meta)\b", re.I)


def _hits(text: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]) -> list[dict[str, Any]]:
    """返回命中规则与位置（起止字符区间），**不返回原文**。"""
    out: list[dict[str, Any]] = []
    for rule, pattern in patterns:
        for match in pattern.finditer(text):
            out.append({"rule": rule, "start": match.start(), "end": match.end()})
            if len(out) >= 40:  # 有界，避免异常文件拖垮扫描
                return out
    return out


def scan(text: str, *, allowed_tools: list[str] | None = None) -> dict[str, Any]:
    """对技能正文做确定性安全扫描，返回结构化结果（可安全写入事件与审计）。"""
    body = str(text or "")
    findings: dict[str, list[dict[str, Any]]] = {
        "injection": _hits(body, _INJECTION_PATTERNS),
        "secrets": _hits(body, _SECRET_PATTERNS),
        "executable": _hits(body, _EXECUTABLE_PATTERNS),
        "html": _hits(body, (("html_container", _HTML_PATTERN),)),
        "tool_authorization": [],
    }
    # 技能只能声明只读工具：出现写/命令类授权视为越权请求
    risky_tools = [
        t for t in (allowed_tools or [])
        if str(t).lower() in ("bash", "shell", "write_file", "edit_file", "delete_file", "exec")
    ]
    if risky_tools:
        findings["tool_authorization"] = [{"rule": "risky_tool", "tool": t} for t in risky_tools]
    size = len(body.encode("utf-8"))
    too_large = size > MAX_SKILL_BYTES
    blocked = bool(
        findings["injection"] or findings["secrets"] or findings["executable"]
        or findings["html"] or findings["tool_authorization"] or too_large
    )
    reasons: list[str] = []
    if findings["injection"]:
        reasons.append("检测到提示注入或控制覆盖")
    if findings["secrets"]:
        reasons.append("检测到疑似凭据或密钥")
    if findings["executable"]:
        reasons.append("检测到脚本/危险命令（技能不允许安装可执行内容）")
    if findings["html"]:
        reasons.append("检测到 HTML/外链容器（技能只允许纯提示词文本）")
    if findings["tool_authorization"]:
        reasons.append("技能声明了高风险工具授权")
    if too_large:
        reasons.append(f"技能正文超过大小上限（{MAX_SKILL_BYTES // 1024}KB）")
    return {
        "ok": not blocked,
        "blocked": blocked,
        "reasons": reasons,
        "findings": findings,
        "bytes": size,
        "rulesHit": sorted({f["rule"] for group in findings.values() for f in group}),
    }


# ---------------------------------------------------------------------------
# 暂存
# ---------------------------------------------------------------------------

def stage(
    *, name: str, prompt_md: str, description: str = "", allowed_tools: list[str] | None = None,
    source_origin: str = "local://paste", base_version_id: str | None = None,
) -> dict[str, Any]:
    """写入技能暂存区并执行扫描；危险项标记 blocked（不可强制放行）。"""
    name = str(name or "").strip()
    body = str(prompt_md or "")
    if not name:
        raise SkillLifecycleError("技能暂存需要 name")
    if not body.strip():
        raise SkillLifecycleError("技能暂存需要非空提示词正文")
    raw = body.encode("utf-8")
    if len(raw) > MAX_SKILL_BYTES:
        raise SkillLifecycleError(f"技能文件超过大小上限（{MAX_SKILL_BYTES // 1024}KB）")
    tools = [str(t) for t in (allowed_tools or [])]
    scan_result = scan(body, allowed_tools=tools)
    db = get_profile_db()
    staging_id = str(uuid.uuid4())
    ts = now_iso()
    sha = hashlib.sha256(raw).hexdigest()
    existing = db.query_one("SELECT id FROM skills WHERE name = ?", (name,))
    base_hash = ""
    if existing is not None:
        base_hash = active_version_hash(str(existing["id"])) or legacy_hash(str(existing["id"]))
        if base_hash == sha and not scan_result["blocked"]:
            # 与当前激活版本完全一致：幂等，无需重复暂存
            return {
                "id": None, "name": name, "status": "unchanged",
                "contentSha256": sha, "scan": scan_result, "reason": "identical_to_active",
            }
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO skill_staging(
                id, name, content_sha256, prompt_md, allowed_tools_json, description,
                source_origin, base_version_id, base_hash, scan_json, status, block_reason,
                created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (staging_id, name, sha, body, json.dumps(tools), str(description or "")[:500],
             str(source_origin or "local://paste")[:200], base_version_id, base_hash,
             json.dumps(scan_result, ensure_ascii=False),
             BLOCKED if scan_result["blocked"] else STAGING,
             "; ".join(scan_result["reasons"]) if scan_result["blocked"] else None,
             ts, ts),
        )
        from . import audit

        audit.append(
            conn, action="skill.stage", resource_type="skill_staging", resource_id=staging_id,
            result="denied" if scan_result["blocked"] else "ok",
            metadata={"name": name[:120], "sha256": sha, "blocked": scan_result["blocked"],
                      "rulesHit": scan_result["rulesHit"]},
        )
    return get_staging(staging_id)


def get_staging(staging_id: str) -> dict[str, Any]:
    row = get_profile_db().query_one("SELECT * FROM skill_staging WHERE id = ?", (staging_id,))
    if row is None:
        raise SkillLifecycleError("暂存记录不存在")
    return _staging_row(row)


def _staging_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "contentSha256": row["content_sha256"],
        "promptMd": row["prompt_md"],
        "description": row["description"],
        "allowedTools": json.loads(row["allowed_tools_json"] or "[]"),
        "sourceOrigin": row["source_origin"],
        "baseVersionId": row["base_version_id"],
        "baseHash": row["base_hash"],
        "scan": json.loads(row["scan_json"] or "{}"),
        "status": row["status"],
        "blockReason": row["block_reason"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def list_staging(*, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    db = get_profile_db()
    if status:
        rows = db.query(
            "SELECT * FROM skill_staging WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, int(limit)),
        )
    else:
        rows = db.query(
            "SELECT * FROM skill_staging ORDER BY created_at DESC LIMIT ?", (int(limit),)
        )
    # 列表不返回 promptMd（正文可能较大且含敏感信息）
    out: list[dict[str, Any]] = []
    for r in rows:
        item = _staging_row(r)
        item.pop("promptMd", None)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 批准 / 拒绝 / 版本
# ---------------------------------------------------------------------------

def approve(staging_id: str, *, note: str | None = None) -> dict[str, Any]:
    """批准暂存：创建新版本并激活；blocked 项不可批准（无强制放行入口）。"""
    record = get_staging(staging_id)
    if record["status"] == BLOCKED:
        raise SkillLifecycleError(
            f"该技能存在安全风险，不能批准：{record['blockReason'] or '扫描未通过'}"
        )
    if record["status"] != STAGING:
        raise SkillLifecycleError(f"暂存状态为 {record['status']}，不能批准")
    db = get_profile_db()
    name = record["name"]
    existing = db.query_one("SELECT * FROM skills WHERE name = ?", (name,))
    ts = now_iso()
    if existing is None:
        skill_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO skills(id, name, description, source_url, prompt_md, "
                "allowed_tools_json, content_sha256, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (skill_id, name, record["description"], record["sourceOrigin"],
                 record["promptMd"], json.dumps(record["allowedTools"]),
                 record["contentSha256"], ts, ts),
            )
            conn.execute(
                "INSERT INTO skill_versions(id, skill_id, version_no, content_sha256, prompt_md, "
                "allowed_tools_json, description, source_origin, base_version_id, scan_json, "
                "active, note, created_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (version_id, skill_id, record["contentSha256"], record["promptMd"],
                 json.dumps(record["allowedTools"]), record["description"],
                 record["sourceOrigin"], record["baseVersionId"],
                 json.dumps(record["scan"], ensure_ascii=False), note, ts),
            )
            conn.execute(
                "UPDATE skill_staging SET status = 'approved', updated_at = ? WHERE id = ?",
                (ts, staging_id),
            )
            _audit(conn, "skill.approve", skill_id, {"stagingId": staging_id,
                                                     "versionNo": 1})
    else:
        skill_id = str(existing["id"])
        current_hash = active_version_hash(skill_id) or legacy_hash(skill_id)
        if current_hash == record["contentSha256"]:
            raise SkillLifecycleError("内容与当前激活版本一致，无需新建版本")
        # baseHash 冲突：暂存基于旧版本生成，必须重新审查
        if record["baseHash"] and record["baseHash"] != current_hash:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE skill_staging SET status = 'rejected', block_reason = ?, "
                    "updated_at = ? WHERE id = ?",
                    ("基线版本已变化，需重新暂存", ts, staging_id),
                )
                _audit(conn, "skill.reject", skill_id,
                       {"stagingId": staging_id, "reason": "base_conflict"})
            raise SkillLifecycleError("技能基线版本已变化：请重新暂存后再批准")
        version_no = int((db.query_one(
            "SELECT COALESCE(MAX(version_no), 0) AS v FROM skill_versions WHERE skill_id = ?",
            (skill_id,),
        ) or {"v": 0})["v"] or 0) + 1
        version_id = str(uuid.uuid4())
        with db.transaction() as conn:
            conn.execute("UPDATE skill_versions SET active = 0 WHERE skill_id = ?", (skill_id,))
            conn.execute(
                "INSERT INTO skill_versions(id, skill_id, version_no, content_sha256, prompt_md, "
                "allowed_tools_json, description, source_origin, base_version_id, scan_json, "
                "active, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (version_id, skill_id, version_no, record["contentSha256"], record["promptMd"],
                 json.dumps(record["allowedTools"]), record["description"],
                 record["sourceOrigin"], record["baseVersionId"],
                 json.dumps(record["scan"], ensure_ascii=False), note, ts),
            )
            # 同步 skills 当前值（只影响该技能，不触碰案件、记忆与授权）
            conn.execute(
                "UPDATE skills SET prompt_md = ?, content_sha256 = ?, description = ?, "
                "allowed_tools_json = ?, updated_at = ? WHERE id = ?",
                (record["promptMd"], record["contentSha256"], record["description"],
                 json.dumps(record["allowedTools"]), ts, skill_id),
            )
            conn.execute(
                "UPDATE skill_staging SET status = 'approved', updated_at = ? WHERE id = ?",
                (ts, staging_id),
            )
            _audit(conn, "skill.approve", skill_id,
                   {"stagingId": staging_id, "versionNo": version_no})
    return {"skillId": skill_id, "versionId": version_id, "name": name, "status": APPROVED}


def reject(staging_id: str, *, note: str | None = None) -> dict[str, Any]:
    record = get_staging(staging_id)
    if record["status"] not in (STAGING, BLOCKED):
        raise SkillLifecycleError(f"暂存状态为 {record['status']}，不能拒绝")
    with get_profile_db().transaction() as conn:
        conn.execute(
            "UPDATE skill_staging SET status = 'rejected', block_reason = ?, updated_at = ? "
            "WHERE id = ?",
            (note or record["blockReason"], now_iso(), staging_id),
        )
        _audit(conn, "skill.reject", None, {"stagingId": staging_id})
    return get_staging(staging_id)


def active_version_hash(skill_id: str) -> str:
    row = get_profile_db().query_one(
        "SELECT content_sha256 FROM skill_versions WHERE skill_id = ? AND active = 1 "
        "ORDER BY version_no DESC LIMIT 1",
        (skill_id,),
    )
    return str(row["content_sha256"]) if row else ""


def legacy_hash(skill_id: str) -> str:
    row = get_profile_db().query_one(
        "SELECT content_sha256 FROM skills WHERE id = ?", (skill_id,)
    )
    return str(row["content_sha256"]) if row else ""


def list_versions(skill_id: str) -> list[dict[str, Any]]:
    rows = get_profile_db().query(
        "SELECT * FROM skill_versions WHERE skill_id = ? ORDER BY version_no DESC",
        (skill_id,),
    )
    return [
        {
            "versionId": r["id"], "skillId": r["skill_id"], "versionNo": int(r["version_no"]),
            "contentSha256": r["content_sha256"], "promptMd": r["prompt_md"],
            "allowedTools": json.loads(r["allowed_tools_json"] or "[]"),
            "description": r["description"], "sourceOrigin": r["source_origin"],
            "baseVersionId": r["base_version_id"], "scan": json.loads(r["scan_json"] or "{}"),
            "active": bool(r["active"]), "note": r["note"], "createdAt": r["created_at"],
        }
        for r in rows
    ]


def diff(skill_id: str, from_version: int, to_version: int) -> dict[str, Any]:
    """两版技能的差异（逐行 unified 风格，只含提示词正文）。"""
    import difflib

    versions = {v["versionNo"]: v for v in list_versions(skill_id)}
    if from_version not in versions or to_version not in versions:
        raise SkillLifecycleError("版本不存在")
    old = str(versions[from_version]["promptMd"]).splitlines(keepends=True)
    new = str(versions[to_version]["promptMd"]).splitlines(keepends=True)
    lines = list(difflib.unified_diff(
        old, new,
        fromfile=f"v{from_version}", tofile=f"v{to_version}", lineterm="",
    ))
    return {
        "skillId": skill_id, "fromVersion": from_version, "toVersion": to_version,
        "diff": "\n".join(lines),
        "oldSha256": versions[from_version]["contentSha256"],
        "newSha256": versions[to_version]["contentSha256"],
    }


def rollback(skill_id: str, version_no: int, *, note: str | None = None) -> dict[str, Any]:
    """回滚到指定版本：只恢复该技能提示词，不影响案件、记忆与工具授权。"""
    db = get_profile_db()
    target = db.query_one(
        "SELECT * FROM skill_versions WHERE skill_id = ? AND version_no = ?",
        (skill_id, int(version_no)),
    )
    if target is None:
        raise SkillLifecycleError("目标版本不存在")
    ts = now_iso()
    with db.transaction() as conn:
        conn.execute("UPDATE skill_versions SET active = 0 WHERE skill_id = ?", (skill_id,))
        conn.execute("UPDATE skill_versions SET active = 1 WHERE id = ?", (target["id"],))
        conn.execute(
            "UPDATE skills SET prompt_md = ?, content_sha256 = ?, description = ?, "
            "allowed_tools_json = ?, updated_at = ? WHERE id = ?",
            (target["prompt_md"], target["content_sha256"], target["description"],
             target["allowed_tools_json"], ts, skill_id),
        )
        _audit(conn, "skill.rollback", skill_id,
               {"versionNo": int(version_no), "note": (note or "")[:200]})
    return {"skillId": skill_id, "activeVersion": int(version_no), "rolledBack": True}


def apply_approved_skill_version(
    skill_id: str, *, name: str, prompt_md: str, note: str | None = None,
) -> dict[str, Any]:
    """把已批准的学习提案落地为技能新版本（与 staging 批准共用版本逻辑）。

    提案来源必须是人工批准，因此这里不再重复扫描；扫描结果在提案阶段已记录。
    """
    db = get_profile_db()
    row = db.query_one("SELECT id FROM skills WHERE id = ?", (skill_id,))
    if row is None:
        raise SkillLifecycleError("技能不存在")
    scan_result = scan(prompt_md)
    if scan_result["blocked"]:
        raise SkillLifecycleError(f"技能正文未通过安全扫描：{'；'.join(scan_result['reasons'])}")
    version_no = int((db.query_one(
        "SELECT COALESCE(MAX(version_no), 0) AS v FROM skill_versions WHERE skill_id = ?",
        (skill_id,),
    ) or {"v": 0})["v"] or 0) + 1
    version_id = str(uuid.uuid4())
    ts = now_iso()
    sha = hashlib.sha256(prompt_md.encode("utf-8")).hexdigest()
    with db.transaction() as conn:
        conn.execute("UPDATE skill_versions SET active = 0 WHERE skill_id = ?", (skill_id,))
        conn.execute(
            "INSERT INTO skill_versions(id, skill_id, version_no, content_sha256, prompt_md, "
            "allowed_tools_json, description, source_origin, scan_json, active, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, '[]', ?, 'learning://approved', ?, 1, ?, ?)",
            (version_id, skill_id, version_no, sha, prompt_md, name,
             json.dumps(scan_result, ensure_ascii=False), note, ts),
        )
        conn.execute(
            "UPDATE skills SET prompt_md = ?, content_sha256 = ?, updated_at = ? WHERE id = ?",
            (prompt_md, sha, ts, skill_id),
        )
        _audit(conn, "skill.apply_proposal", skill_id,
               {"versionNo": version_no, "sha256": sha})
    return {"skillId": skill_id, "versionId": version_id, "versionNo": version_no}


def _audit(conn: Any, action: str, skill_id: str | None, metadata: dict[str, Any]) -> None:
    from . import audit

    audit.append(
        conn, action=action, resource_type="skill", resource_id=str(skill_id or ""),
        metadata=metadata,
    )
