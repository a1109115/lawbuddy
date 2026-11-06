"""技能（Skill）安装与管理（REQ-AI-003 / REQ-AI-007）。

- 技能 = 提示词包（Markdown + frontmatter），非可执行代码；
  能够执行代码的扩展不走此通道（技术方案 §7：默认拒绝插件安装）；
- 安装来源是用户在聊天中提供的 https URL，由 Agent 调 skill_install 工具
  或 service 层 RPC 触发；安全边界：
    * 仅接受 https（重定向每一跳复检）；
    * 内容上限 256KB（Content-Length 预检 + 读时截断双保险）；
    * frontmatter 手写解析，两级策略：严格 SKILL.md 优先（任何字段缺失/
      非法即拒绝）；失败时按 Skill Hub 页面宽松兜底——容忍无 frontmatter、
      连字符字段等方言，name 可回退正文一级标题/URL slug，正文超 64KB
      截断并注明来源；两种路径都拒绝 HTML/XML 与空正文；
    * allowedTools 仅作为声明性元数据保存，不做工具限制（工具开放由
      Agent Loop 统一决定）；
    * prompt_md 上限 64KB（严格路径超限拒绝，宽松路径截断）；
    * 同 name 安装：内容 sha256 相同幂等返回，不同则拒绝（先删再装）；
- 技能内容在安装时经 HTTP 快照落库（本地优先），source_url 始终保留，
  可按 URL 重新获取完整内容；使用时注入快照，不需要本地文件。
- 技能存放在设备级配置目录（固定位置，跨工作空间共享），不随工作空间切换丢失。
  文件通道：Agent 可用沙箱文件工具把技能写到当前工作空间的
  skills/<分组>/<技能名>/SKILL.md（例如照技能商店安装说明落盘），list_skills
  时自动提升到设备级目录并同步进库，source_url 标记为
  workspace://skills/<相对路径>，文件删除即从列表移除。
- 审计只记来源标识（域名或 workspace 相对路径）、内容 sha256、长度与结果，
  不落技能正文。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import ssl
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from .audit import log_standalone_profile
from .storage import get_profile_db
from .workspace import now_iso

MAX_CONTENT_BYTES = 256 * 1024
MAX_PROMPT_CHARS = 64 * 1024
FETCH_TIMEOUT_S = 30
SKILLS_DIRNAME = "skills"                      # 设备级技能目录（固定位置，跨工作空间共享）
WORKSPACE_SKILL_PREFIX = "workspace://skills/"  # 技能文件的 source_url 前缀
MAX_WORKSPACE_SKILL_FILES = 200                 # 单次同步扫描上限（防异常目录拖垮列表）


class SkillError(RuntimeError):
    """技能解析/校验/安装失败。"""


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """解析 ``---\\n...\\n---`` frontmatter，返回 (元数据, 提示词正文)。

    只支持简单语法：``key: value`` 与行式列表 ``[a, b]`` / ``- item``；
    任何异常结构一律抛 SkillError（fail-closed）。
    """
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        raise SkillError("技能文件必须以 --- 开头的 frontmatter 开始")
    end = text.find("\n---", 4)
    if end < 0:
        raise SkillError("frontmatter 未闭合（缺少结束 ---）")
    head = text[4:end]
    body = text[end + 4:]
    if body.startswith("\n"):
        body = body[1:]

    meta: dict[str, Any] = {}
    current_list_key: str | None = None
    for line_no, line in enumerate(head.split("\n"), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            if current_list_key is None:
                raise SkillError(f"frontmatter 第 {line_no} 行：列表项缺少所属字段")
            meta[current_list_key].append(_clean_scalar(stripped[2:]))
            continue
        if ":" not in stripped:
            raise SkillError(f"frontmatter 第 {line_no} 行缺少冒号")
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalpha():
            raise SkillError(f"frontmatter 第 {line_no} 行字段名非法")
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            meta[key] = [_clean_scalar(v) for v in inner.split(",")] if inner else []
            current_list_key = key
        elif value == "":
            meta[key] = []
            current_list_key = key
        else:
            meta[key] = _clean_scalar(value)
            current_list_key = None
    return meta, body


def _clean_scalar(value: str) -> str:
    value = value.strip().strip("'\"").strip()
    return value


def _parse_allowed_tools(value: Any) -> list[str]:
    """规范化 allowedTools 声明（仅去重，不校验工具名——声明是元数据不是限制）。"""
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",") if v.strip()]
    elif isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
    else:
        items = []
    seen: list[str] = []
    for t in items:
        if t not in seen:
            seen.append(t)
    return seen


def _looks_like_html_or_xml(text: str) -> bool:
    head = text[:2048].lstrip().lower()
    return head.startswith(("<!doctype html", "<html", "<?xml"))


def _slug_from_url(url: str) -> str:
    """从 URL 推导技能名：优先 variant/skill/name/slug 查询参数，其次路径末段。"""
    parsed = urlparse(url)
    slug = ""
    try:
        for key, value in parse_qsl(parsed.query):
            if key.lower() in ("variant", "skill", "name", "slug", "id") and value:
                slug = value
                break
    except ValueError:
        pass
    if not slug:
        tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        slug = tail[:-3] if tail.lower().endswith(".md") else tail
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", unquote(slug)).strip("-.")
    return cleaned[:60]


def _parse_lenient_page(raw: str, url: str) -> tuple[dict[str, Any], str]:
    """Skill Hub 页面宽松解析（严格 SKILL.md 解析失败时的兜底）。

    技能商店页面通常是清单/安装说明式 Markdown：可能没有 frontmatter，或使用
    本应用严格解析器不支持的方言（连字符字段、未知键、嵌套结构等）。这里容忍
    这些差异并推导元数据，让商店技能也能进入技能列表（使用时按快照/URL 走
    HTTP 内容）；安全边界不变：
        * 拒绝 HTML/XML 响应（防止把网页或错误页误装成技能）；
        * name 回退顺序：frontmatter name/title/slug → 正文一级标题 → URL slug；
        * description 回退正文第一个非标题/列表行；
        * 正文超过 64KB 上限时截断并注明，完整内容按 source_url 获取。
    """
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if _looks_like_html_or_xml(text):
        raise SkillError("该地址返回的是网页/XML 而非 Markdown 技能文件；请在技能页复制 .md 原始链接后重试")

    meta: dict[str, Any] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            head, body = text[4:end], text[end + 4:]
            if body.startswith("\n"):
                body = body[1:]
            current_list_key: str | None = None
            for line in head.split("\n"):
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("- "):
                    if current_list_key is not None:
                        meta[current_list_key].append(_clean_scalar(stripped[2:]))
                    continue
                if ":" not in stripped:
                    current_list_key = None
                    continue
                key = stripped.partition(":")[0].strip().lower().replace("-", "_")
                value = stripped.partition(":")[2].strip()
                if not key or not key.replace("_", "").isalpha() or key in meta:
                    # 非法键或重复键：宽松模式忽略（首次出现优先，顶层不被嵌套覆盖）
                    continue
                if value.startswith("[") and value.endswith("]"):
                    inner = value[1:-1].strip()
                    meta[key] = [_clean_scalar(v) for v in inner.split(",")] if inner else []
                    current_list_key = key
                elif value:
                    meta[key] = _clean_scalar(value)
                    current_list_key = key
                else:
                    meta[key] = []
                    current_list_key = key

    # 字段别名归一（allowed-tools / allowedtools、title / slug 等）
    if "allowed_tools" not in meta:
        for alias in ("allowedtools", "allowed_tools_json"):
            if meta.get(alias):
                meta["allowed_tools"] = meta[alias]
                break
    for alias in ("title", "slug"):
        if not str(meta.get("name") or "") and meta.get(alias):
            meta["name"] = meta[alias]
    for key in ("name", "title", "description"):
        if isinstance(meta.get(key), list):
            meta[key] = " ".join(str(v) for v in meta[key])

    if not str(meta.get("name") or "").strip():
        for line in body.split("\n"):
            s = line.strip()
            if s.startswith("# ") and s[2:].strip():
                meta["name"] = s[2:].strip()
                break
    if not str(meta.get("name") or "").strip():
        meta["name"] = _slug_from_url(url)
    name_val = str(meta.get("name") or "").strip()
    if not name_val:
        raise SkillError("无法确定技能名称；请确认页面包含 name 字段、一级标题或可识别的 URL")
    if len(name_val) > 60:
        meta["name"] = name_val[:60]

    if not str(meta.get("description") or "").strip():
        for line in body.split("\n"):
            s = line.strip()
            if not s or s.startswith(("#", "-", ">", "|", "```", "!", "[")):
                continue
            meta["description"] = s
            break

    if len(body) > MAX_PROMPT_CHARS:
        body = (
            body[:MAX_PROMPT_CHARS]
            + "\n\n[注意：原始页面超过 64KB 上限，以上内容已截断；完整内容请按来源 URL 获取]"
        )
    return meta, body


def _build_skill_fields(meta: dict[str, Any], prompt_md: str) -> tuple[str, str, list[str]]:
    """从解析结果提取入库字段（name/description/allowedTools），非法即抛 SkillError。

    URL 安装与工作空间同步共用同一套字段语义，保证两条通道行为一致。
    """
    name = str(meta.get("name") or "").strip()
    if not name or len(name) > 60:
        raise SkillError("frontmatter 缺少合法 name（≤60 字符）")
    description = str(meta.get("description") or "").strip()[:300]
    allowed_tools = _parse_allowed_tools(meta.get("allowedTools") or meta.get("allowed_tools"))
    if not prompt_md.strip():
        raise SkillError("技能提示词正文为空")
    if len(prompt_md) > MAX_PROMPT_CHARS:
        raise SkillError(f"技能提示词超过 {MAX_PROMPT_CHARS // 1024}KB 上限")
    return name, description, allowed_tools


def install_from_text(raw: str, source: str = "local://paste") -> dict[str, Any]:
    """从本地 SKILL.md 文本安装技能；同 name 同内容幂等，同 name 不同内容拒绝。"""
    data = str(raw or "").encode("utf-8")
    if len(data) > MAX_CONTENT_BYTES:
        raise SkillError(f"技能文件超过大小上限（{MAX_CONTENT_BYTES // 1024}KB）")
    content_hash = hashlib.sha256(data).hexdigest()
    text = str(raw or "")

    # 严格 SKILL.md 优先；失败时按宽松解析兜底（见 _parse_lenient_page）
    try:
        meta, prompt_md = parse_frontmatter(text)
    except SkillError:
        meta, prompt_md = _parse_lenient_page(text, source)

    name, description, allowed_tools = _build_skill_fields(meta, prompt_md)

    from .storage import get_profile_db

    db = get_profile_db()
    origin = str(source or "local://paste")[:200]
    existing = db.query_one("SELECT * FROM skills WHERE name = ?", (name,))
    if existing is not None:
        if existing["content_sha256"] == content_hash:
            log_standalone_profile(
                action="skill.install", resource_type="skill", resource_id=str(existing["id"]),
                result="ok", metadata={"idempotent": True, "origin": origin, "sha256": content_hash},
            )
            return _row_to_skill(existing)
        log_standalone_profile(
            action="skill.install", resource_type="skill", resource_id=str(existing["id"]),
            result="failed", metadata={"reason": "name_conflict", "origin": origin},
        )
        raise SkillError(f"已存在同名技能「{name}」且内容不同；请先删除再安装")

    skill_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO skills(id, name, description, source_url, prompt_md, allowed_tools_json,"
            " content_sha256, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (skill_id, name, description, origin, prompt_md, json.dumps(allowed_tools), content_hash, now_iso(), now_iso()),
        )
        from . import audit

        audit.append(
            conn, action="skill.install", resource_type="skill", resource_id=skill_id,
            metadata={"name": name, "origin": origin, "sha256": content_hash, "bytes": len(data)},
        )
    return {"id": skill_id, "name": name, "description": description, "allowedTools": allowed_tools}


def _row_to_skill(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "sourceUrl": row["source_url"],
        "promptMd": row["prompt_md"],
        "allowedTools": json.loads(row["allowed_tools_json"] or "[]"),
        "contentSha256": row["content_sha256"],
        "enabled": bool(row["enabled"]),
        "createdAt": row["created_at"],
    }


def _iter_workspace_skill_files(root: Path) -> list[tuple[Path, str]]:
    """枚举 skills/**/SKILL.md，返回 (文件路径, 技能目录相对路径)。

    技能必须放在自己的子目录（如 skills/@org/case-review/SKILL.md），
    相对路径作为 workspace:// 来源标识的一部分。root 现在是设备级配置目录
    （固定位置），不再随工作空间切换。
    """
    skills_dir = root / SKILLS_DIRNAME
    if not skills_dir.is_dir():
        return []
    found: list[tuple[Path, str]] = []
    for path in sorted(skills_dir.rglob("SKILL.md")):
        rel = path.parent.relative_to(skills_dir).as_posix()
        found.append((path, rel))
        if len(found) >= MAX_WORKSPACE_SKILL_FILES:
            break
    return found


def _remove_workspace_skill_dir(source_url: str) -> bool:
    """删除技能文件目录（含 SKILL.md 与配套资源）。

    UI/Agent 移除技能时若不删目录，下次同步会立刻"复活"；这里连目录一起删，
    并向上清理清空的父目录（如 skills/@org/）。会同时清理设备级技能目录与当前
    工作空间里的同名副本（后者是提升来源），只允许删各自 skills/ 内的子路径。
    """
    if not source_url.startswith(WORKSPACE_SKILL_PREFIX):
        return False
    rel = source_url[len(WORKSPACE_SKILL_PREFIX):].strip("/")
    if not rel:
        return False
    from .profile import skills_root
    from .workspace import SKILLS_DIRNAME as WS_SKILLS_DIRNAME

    try:
        root = skills_root().resolve()
    except RuntimeError:
        return False
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return False
    if target == root or not target.is_dir():
        return False
    _rmdir_tree(target, root)

    # 工作空间里的提升来源一并删除，避免下次同步再次提升回来
    try:
        from .sandbox import workspace_root

        ws_root = workspace_root() / WS_SKILLS_DIRNAME
        ws_target = (ws_root / rel).resolve()
        if ws_target.is_dir():
            _rmdir_tree(ws_target, ws_root.resolve())
    except Exception:  # noqa: BLE001 — 工作空间未初始化时忽略
        pass
    return True


def _rmdir_tree(target: Path, stop_at: Path) -> None:
    """删除目录并向上清理空父目录，直到 stop_at 为止。"""
    shutil.rmtree(target, ignore_errors=True)
    parent = target.parent
    while parent != stop_at and stop_at in parent.parents:
        try:
            parent.rmdir()  # 只能删空目录，非空即停
        except OSError:
            break
        parent = parent.parent


def _promoted_manifest(profile_dir: Path, ws_root: Path) -> Path:
    """提升清单按工作空间分开存放（同机多个工作空间互不影响）。

    单份全局清单会让「切到一个还没有 skills/ 的新工作空间」被误判成
    「原工作空间的技能文件已被删除」，从而把设备级技能文件和库记录一起清掉。
    """
    try:
        key = str(ws_root.resolve())
    except OSError:
        key = str(ws_root)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return profile_dir / ".promoted_skills" / f"{digest}.json"


def _promote_workspace_skills(profile_dir: Path) -> int:
    """把当前工作空间 `skills/**/SKILL.md` 提升到设备级技能目录。

    Agent 只能用沙箱文件工具写工作空间内的文件，因此「照安装说明把技能落盘到
    skills/<分组>/<技能名>/SKILL.md」仍然可行；这些文件在同步时复制到设备级
    skills 目录，从而与 URL 安装一样跨工作空间保留。语义：
    - 设备级没有对应文件 → 复制；
    - 内容有变化 → 覆盖（工作空间里的编辑即时生效）；
    - 工作空间侧文件被删除 → 同步删除此前提升的副本（不残留失效技能）。
    已提升的相对路径按工作空间记在 `<profile>/.promoted_skills/<hash>.json`，
    用于区分「由该工作空间提升」与「直接装在设备级 / 由其他工作空间提升」的
    技能文件——切换工作空间不会把别处的技能当成「已删除」清掉。
    """
    from .workspace import SKILLS_DIRNAME as WS_SKILLS_DIRNAME

    try:
        from .sandbox import workspace_root

        ws_root = workspace_root()
    except Exception:  # noqa: BLE001 — 工作空间未初始化时不提升
        return 0
    src_root = ws_root / WS_SKILLS_DIRNAME
    dst_root = profile_dir / SKILLS_DIRNAME
    manifest_path = _promoted_manifest(profile_dir, ws_root)
    try:
        promoted_prev = set(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        promoted_prev = set()

    current: set[str] = set()
    promoted = 0
    if src_root.is_dir():
        for src in sorted(src_root.rglob("*")):
            if not src.is_file() or src.name.startswith("."):
                continue
            rel = src.relative_to(src_root).as_posix()
            target = dst_root / rel
            try:
                data = src.read_bytes()
            except OSError:
                continue
            current.add(rel)
            if target.exists() and target.read_bytes() == data:
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                if src.name == "SKILL.md":
                    promoted += 1
            except OSError:
                continue

    # 工作空间侧已删除的文件：清理此前提升的副本与其空目录
    for rel in sorted(promoted_prev - current):
        target = dst_root / rel
        try:
            if target.is_file():
                target.unlink()
            parent = target.parent
            while parent != dst_root:
                parent.rmdir()  # 只能删空目录，非空即停
                parent = parent.parent
        except OSError:
            pass

    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(sorted(current), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return promoted


def sync_workspace_skills() -> dict[str, int]:
    """把设备级 skills/**/SKILL.md 同步进技能库（list_skills 时自动触发）。

    技能文件存放于设备级配置目录（固定位置，跨工作空间共享）。Agent 仍可把
    技能写到当前工作空间的 skills/<分组>/<技能名>/SKILL.md，同步时会先提升到
    设备级目录，再与 URL 安装一样出现在技能列表。语义：
    - 新文件 → 注册（source_url = workspace://skills/<相对路径>，默认启用）；
    - 同名同源（workspace://）内容变化 → 就地更新（文件改了列表跟着变）；
    - 同名但来源是 https URL → URL 安装优先，跳过不覆盖；
    - 文件/目录被删除 → 对应 workspace:// 技能从库中移除；
    - 单个文件超限/解析失败只跳过，绝不让列表不可用。
    返回 {added, updated, removed, promoted, skipped} 便于调试与评测断言。
    """
    db = get_profile_db()
    from .profile import profile_root

    root = profile_root()
    stats = {"added": 0, "updated": 0, "removed": 0, "promoted": 0, "skipped": 0}
    try:
        stats["promoted"] = _promote_workspace_skills(root)
    except Exception:  # noqa: BLE001 — 提升失败只影响本次，不影响已有技能
        pass
    scanned: set[str] = set()
    for path, rel in _iter_workspace_skill_files(root):
        source_url = f"{WORKSPACE_SKILL_PREFIX}{rel}"
        try:
            data = path.read_bytes()
            if len(data) > MAX_CONTENT_BYTES:
                stats["skipped"] += 1
                continue
            raw = data.decode("utf-8")
            # 与 URL 安装同一套解析：严格 SKILL.md 优先，宽松页面兜底
            try:
                meta, prompt_md = parse_frontmatter(raw)
            except SkillError:
                meta, prompt_md = _parse_lenient_page(raw, source_url)
            name, description, allowed_tools = _build_skill_fields(meta, prompt_md)
        except (OSError, UnicodeDecodeError, SkillError):
            stats["skipped"] += 1
            continue
        scanned.add(source_url)
        content_hash = hashlib.sha256(data).hexdigest()
        existing = db.query_one("SELECT * FROM skills WHERE name = ?", (name,))
        if (
            existing is not None
            and existing["source_url"] != source_url
            and not str(existing["source_url"] or "").startswith(WORKSPACE_SKILL_PREFIX)
        ):
            stats["skipped"] += 1  # https URL 安装的同名技能优先，不被文件覆盖
            continue
        if existing is not None and existing["source_url"] == source_url \
                and existing["content_sha256"] == content_hash:
            continue  # 未变化
        with db.transaction() as conn:
            from . import audit

            if existing is None:
                skill_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO skills(id, name, description, source_url, prompt_md,"
                    " allowed_tools_json, content_sha256, enabled, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (skill_id, name, description, source_url, prompt_md,
                     json.dumps(allowed_tools), content_hash, now_iso(), now_iso()),
                )
                audit.append(
                    conn, action="skill.install", resource_type="skill", resource_id=skill_id,
                    metadata={"name": name, "source": "workspace", "path": rel, "sha256": content_hash},
                )
                stats["added"] += 1
            else:
                conn.execute(
                    "UPDATE skills SET description = ?, source_url = ?, prompt_md = ?,"
                    " allowed_tools_json = ?, content_sha256 = ?, updated_at = ? WHERE id = ?",
                    (description, source_url, prompt_md, json.dumps(allowed_tools),
                     content_hash, now_iso(), existing["id"]),
                )
                audit.append(
                    conn, action="skill.update", resource_type="skill", resource_id=existing["id"],
                    metadata={"name": name, "source": "workspace", "path": rel, "sha256": content_hash},
                )
                stats["updated"] += 1
    # 文件已被删除的 workspace 技能随同步移除，避免列表残留失效项
    stale = db.query("SELECT id, name, source_url FROM skills WHERE source_url LIKE 'workspace://%'")
    for row in stale:
        if str(row["source_url"]) not in scanned:
            with db.transaction() as conn:
                from . import audit

                conn.execute("DELETE FROM skills WHERE id = ?", (row["id"],))
                audit.append(
                    conn, action="skill.remove", resource_type="skill", resource_id=row["id"],
                    metadata={"name": row["name"], "source": "workspace", "reason": "file_missing"},
                )
            stats["removed"] += 1
    return stats


def list_skills() -> list[dict[str, Any]]:
    try:
        sync_workspace_skills()
    except Exception:  # noqa: BLE001 — 同步失败绝不阻断技能列表
        pass
    db = get_profile_db()
    rows = db.query("SELECT * FROM skills ORDER BY created_at")
    return [_row_to_skill(r) for r in rows]


def get_skills(skill_ids: list[str]) -> list[dict[str, Any]]:
    """按 id 列表读取启用的技能（保持传入顺序）。"""
    if not skill_ids:
        return []
    db = get_profile_db()
    rows = db.query("SELECT * FROM skills WHERE enabled = 1")
    by_id = {r["id"]: _row_to_skill(r) for r in rows}
    return [by_id[sid] for sid in skill_ids if sid in by_id]


def validate_skills(skill_ids: list[str]) -> list[str]:
    """校验任务绑定的技能 id：去重、必须存在且启用，返回去重后的 id 列表。"""
    ids: list[str] = []
    for sid in skill_ids or []:
        sid = str(sid).strip()
        if sid and sid not in ids:
            ids.append(sid)
    if not ids:
        return []
    available = {s["id"] for s in get_skills(ids)}
    missing = [sid for sid in ids if sid not in available]
    if missing:
        raise ValueError(f"技能不存在或已禁用：{', '.join(missing[:5])}")
    return ids


def remove_skill(skill_id: str) -> bool:
    db = get_profile_db()
    row = db.query_one("SELECT id, name, source_url FROM skills WHERE id = ?", (str(skill_id),))
    if row is None:
        return False
    dir_removed = False
    try:
        dir_removed = _remove_workspace_skill_dir(str(row["source_url"] or ""))
    except OSError:
        dir_removed = False
    with db.transaction() as conn:
        conn.execute("DELETE FROM skills WHERE id = ?", (row["id"],))
        from . import audit

        audit.append(
            conn, action="skill.remove", resource_type="skill", resource_id=row["id"],
            metadata={"name": row["name"], "workspaceDirRemoved": dir_removed},
        )
    return True
