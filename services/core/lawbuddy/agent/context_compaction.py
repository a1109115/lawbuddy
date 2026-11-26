"""引证安全的上下文压缩（LB-HERMES-2026-01 WP04）。

替代 `autonomous._compact_messages` 的继续膨胀式摘要，分三级管线：

1. **确定性裁剪**：把超大旧工具结果替换为「内容引用」（哈希 + 字节数 + 读取方式），
   保留 tool_call 与 tool 消息的配对、调用 ID 与结果哈希，不删消息；
2. **Token 预算保护**：system 前缀、最近轮次、未提交工作状态与证据索引**永不压缩**，
   压缩只作用于两者之间的中间段；
3. **确定性导航摘要**：中间段生成结构化导航摘要（消息序号、角色、工具名、
   引证 ID 与来源引用），**不复制事实结论**——摘要文本本身不能作为 `supported`
   依据，只能指向 `claim_id/source_ref` 与原文页。

写入保护（§6.2 / WP04）：
- 压缩前完整 transcript 加密归档（`context_archives`）；
- 归档写入使用 `watermark + CAS`：`state["_messages_watermark"]` 与库内最新
  归档的 watermark 不一致时**拒绝写入**并返回未压缩消息，避免过期压缩任务
  覆盖新会话；
- 压缩失败不产生半写入：先归档成功再返回压缩结果，任一环节失败回退原文。

与既有实现的兼容：`autonomous` 仍可调用 `_compact_messages` 作为兜底；
`agent.contextCompressionV2` 关闭时走旧路径，但归档与 CAS 仍然生效。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from ..storage import get_db
from ..workspace import now_iso

# 压缩后保留的最近消息条数（与旧实现一致的下限，可按 Token 预算上调）
KEEP_RECENT_MSGS = 12
# 中间段单条内容摘录上限（字符）
DIGEST_USER_CHARS = 400
DIGEST_ASSISTANT_CHARS = 600
DIGEST_TOOL_CHARS = 280
DIGEST_ARGS_CHARS = 160
# 超大工具结果阈值：超过即替换为内容引用（字符）
BLOB_THRESHOLD_CHARS = 6000
# 归档保留条数（超出后清理最旧，避免无限增长）
ARCHIVE_KEEP = 20


class CompactionError(RuntimeError):
    """压缩失败；调用方必须使用未压缩 canonical 消息继续，不得半写入。"""


class CompactionConflict(CompactionError):
    """watermark 不一致（并发压缩 / 过期压缩任务）；调用方保留新消息。"""


def _messages_chars(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        total += len(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            total += len(str((tc.get("function") or {}).get("arguments") or ""))
    return total


def _clip(text: Any, limit: int) -> str:
    raw = str(text or "").strip()
    return raw if len(raw) <= limit else raw[: max(0, limit - 1)] + "…"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def messages_hash(messages: list[dict[str, Any]]) -> str:
    """消息序列指纹，用作 watermark 与 CAS 依据。"""
    payload = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
    return _sha(payload)


# ---------------------------------------------------------------------------
# 加密归档
# ---------------------------------------------------------------------------

def _archive_dir():
    from ..sandbox import workspace_root

    root = workspace_root() / "context_archives"
    root.mkdir(parents=True, exist_ok=True)
    return root


def archive_transcript(
    task_id: str, messages: list[dict[str, Any]], *, reason: str = "compaction",
) -> dict[str, Any]:
    """压缩前完整 transcript 加密归档，返回归档元数据（不含正文）。

    watermark = 归档前消息指纹；后续压缩必须以它为 CAS 基准。
    """
    from .. import crypto
    from ..keyring import get_active_dek

    raw = json.dumps(messages, ensure_ascii=False, default=str).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    key = crypto.derive_file_key(get_active_dek())
    root = _archive_dir()
    dst = root / f"{task_id}.{sha[:16]}.enc"
    rel = f"context_archives/{dst.name}"
    if not dst.exists():
        tmp = dst.with_suffix(".tmp")
        tmp.write_bytes(crypto.encrypt_bytes(raw, key))
        tmp.replace(dst)
    archive_id = str(uuid.uuid4())
    ts = now_iso()
    with get_db().transaction() as conn:
        conn.execute(
            """
            INSERT INTO context_archives(id, task_id, watermark, content_sha256, byte_size,
                                         message_count, reason, storage_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (archive_id, task_id, sha, sha, len(raw), len(messages), reason, rel, ts),
        )
        # 只保留最近 ARCHIVE_KEEP 个归档元数据（密文文件由工作空间清理策略处理）
        conn.execute(
            """
            DELETE FROM context_archives WHERE task_id = ? AND id NOT IN (
              SELECT id FROM context_archives WHERE task_id = ?
              ORDER BY created_at DESC, id DESC LIMIT ?
            )
            """,
            (task_id, task_id, ARCHIVE_KEEP),
        )
    return {
        "archiveId": archive_id, "watermark": sha, "sha256": sha,
        "bytes": len(raw), "messages": len(messages), "path": rel,
    }


def latest_watermark(task_id: str) -> str | None:
    row = get_db().query_one(
        "SELECT watermark FROM context_archives WHERE task_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (task_id,),
    )
    return str(row["watermark"]) if row else None


def read_archive(task_id: str, archive_id: str) -> list[dict[str, Any]] | None:
    """按归档 ID 解密读取完整 transcript（用于恢复与诊断）。"""
    from .. import crypto
    from ..keyring import get_active_dek
    from ..sandbox import workspace_root

    row = get_db().query_one(
        "SELECT storage_path FROM context_archives WHERE id = ? AND task_id = ?",
        (archive_id, task_id),
    )
    if row is None:
        return None
    path = workspace_root() / str(row["storage_path"])
    if not path.exists():
        return None
    raw = crypto.decrypt_bytes(path, crypto.derive_file_key(get_active_dek()))
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# 第一阶段：超大工具结果 → 内容引用
# ---------------------------------------------------------------------------

def _tool_ref(content: str, *, call_id: str) -> str:
    """工具结果的内容引用：保留调用 ID 与结果哈希，不保留正文。

    正文已随压缩前 transcript 加密归档，需要时可读归档或重新调用工具；
    摘要中的引用**不能**作为事实依据。
    """
    sha = _sha(content)
    return (
        f"（工具结果已归档为内容引用：call_id={call_id or 'unknown'}，"
        f"sha256={sha[:16]}，{len(content)} 字。"
        "需要逐字内容时请重新调用对应工具或读取原文页，不要引用本摘要作为事实依据。）"
    )


# ---------------------------------------------------------------------------
# 第三阶段：确定性导航摘要（只放指引，不放事实结论）
# ---------------------------------------------------------------------------

def _digest_lines(messages: list[dict[str, Any]], start: int, end: int) -> tuple[list[str], int]:
    """中间段导航行 + 超大工具结果转内容引用的计数。

    工具调用与结果配对通过 `call_id` 显式保留：`[调用] name#call_id(...)` 与
    `[结果] call_id → 内容引用(sha256, 字数)`。正文与结果哈希可回查归档，
    摘要本身不承载事实。
    """
    lines: list[str] = []
    slimmed = 0
    for m in messages[start:end]:
        role = m.get("role")
        text = str(m.get("content") or "").strip()
        if role == "user":
            lines.append(f"[用户] {_clip(text, DIGEST_USER_CHARS)}")
        elif role == "assistant":
            if text:
                lines.append(f"[模型] {_clip(text, DIGEST_ASSISTANT_CHARS)}")
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                cid = str(tc.get("id") or "")
                lines.append(
                    f"[调用] {fn.get('name')}#{cid}"
                    f"({_clip(str(fn.get('arguments') or ''), DIGEST_ARGS_CHARS)})"
                )
        elif role == "tool":
            cid = str(m.get("tool_call_id") or "")
            if len(text) > BLOB_THRESHOLD_CHARS:
                # 第一阶段：超大旧结果裁剪为内容引用（保留 call_id 与结果哈希）
                lines.append("[结果] " + _tool_ref(text, call_id=cid))
                slimmed += 1
            else:
                lines.append(f"[结果] {cid} {_clip(text, DIGEST_TOOL_CHARS)}")
    return lines, slimmed


def _evidence_index(state: dict[str, Any]) -> list[str]:
    """证据索引：只列 claim_id + source_ref，不列结论正文（防止摘要成为事实依据）。"""
    ledger = state.get("evidence_ledger") or {}
    out: list[str] = []
    for key, entry in list(ledger.items())[:40]:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source") or {}
        ref = " ".join(str(x) for x in (
            source.get("documentId") or source.get("documentTitle") or "",
            f"p{source.get('pageNo')}" if source.get("pageNo") else "",
            source.get("clauseRef") or "",
        ) if str(x).strip())
        claim_id = str(entry.get("claimId") or key)
        status = str(entry.get("status") or "")
        out.append(f"- {claim_id} [{status or 'unknown'}] {ref}".strip())
    return out


def _open_work_index(state: dict[str, Any]) -> list[str]:
    """未提交工作索引：单元/格状态计数，来自账本而非历史消息。"""
    task_id = str(state.get("task_id") or "")
    if not task_id:
        return []
    try:
        from . import work_ledger

        units = work_ledger.unit_progress(task_id)
        cells = work_ledger.cell_progress(task_id)
    except Exception:  # noqa: BLE001 账本不可用时不注入（不编造进度）
        return []
    lines: list[str] = []
    if units.get("total"):
        lines.append(
            f"- 工作单元：终态 {units['terminal']}/{units['total']}，"
            f"待核实 {units['needsHuman']}，阻塞 {units['blocked']}"
        )
    if cells.get("total"):
        lines.append(
            f"- 答案格：已提交 {cells['committed']}，待核实 {cells['needsHuman']}，"
            f"未处理 {cells['pending']}"
        )
    return lines


def _build_digest(
    state: dict[str, Any], messages: list[dict[str, Any]], start: int, end: int,
) -> tuple[str, int]:
    header = (
        "（系统提示：以下为此前轮次的压缩导航摘要，**不是**用户新发言，也**不是**事实依据。"
        "摘要只保留可导航线索：需要逐字原文时用 document_get_pages 读取；"
        "需要历史结论与引证时用 memory_search 检索；"
        "任何事实性回答都必须重新给出文档 + 页码 + 原文短引，"
        "不得引用本摘要内容作为 supported 依据。）"
    )
    parts = [header]
    evidence = _evidence_index(state)
    if evidence:
        parts.append("【证据索引（claim_id / 状态 / 来源）】\n" + "\n".join(evidence))
    work = _open_work_index(state)
    if work:
        parts.append("【未完成工作状态（来自账本）】\n" + "\n".join(work))
    body, slimmed = _digest_lines(messages, start, end)
    if body:
        parts.append("【历史轮次导航】\n" + "\n".join(body))
    return "\n\n".join(parts), slimmed


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _find_boundary(messages: list[dict[str, Any]], keep_recent: int) -> int:
    """确定可压缩段的结束位置：回退到 assistant，保证 tool 消息与 tool_calls 配对。"""
    start = max(1, len(messages) - keep_recent)
    while start > 1 and messages[start].get("role") != "assistant":
        start -= 1
    return start


def compact(
    state: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = KEEP_RECENT_MSGS,
    force: bool = False,
    archive: bool = True,
    expected_watermark: str | None = None,
) -> dict[str, Any]:
    """执行一次引证安全压缩。

    `expected_watermark` 是 CAS 检查点：调用方声明"我认为当前消息就是这一版"。
    传入时若与 `messages` 指纹不一致，说明调用方持有的是过期视图（例如从旧检查点
    恢复的过期压缩 Worker），此时拒绝压缩并原样返回，避免旧压缩覆盖新会话。

    返回：
        {
          "messages": [...],          # 压缩后消息（失败时为原消息）
          "stats": {...},             # 前后字符数、压缩条数、替换的工具结果数
          "archive": {...} | None,    # 加密归档元数据
          "watermark": str,           # 输入消息指纹（下次 CAS 的基准）
          "applied": bool,            # 是否实际压缩
          "reason": str,              # 未压缩原因（skipped / conflict / error）
        }

    **永不抛异常**：任何失败都返回未压缩 canonical 消息，由调用方继续。
    """
    before_chars = _messages_chars(messages)
    stats: dict[str, Any] = {
        "before": before_chars, "after": before_chars, "compactedMessages": 0,
        "slimmedToolResults": 0, "mode": "none",
    }
    base = {
        "messages": messages, "stats": stats, "archive": None,
        "watermark": messages_hash(messages), "applied": False,
    }
    task_id = str(state.get("task_id") or "")
    if not task_id:
        return {**base, "reason": "no_task"}
    if len(messages) <= keep_recent + 2:
        return {**base, "reason": "too_few_messages"}

    boundary = _find_boundary(messages, keep_recent)
    if boundary <= 1:
        return {**base, "reason": "no_compactable_segment"}

    # 1) CAS 基准检查（必须在归档前，否则刚写入的归档会让检查永远通过）
    #    - `expected_watermark`：调用方视图比对（过期压缩 Worker 保护）；
    #    - 库内最新归档：并发压缩已推进会话时，本次不得覆盖。
    watermark = messages_hash(messages)
    if expected_watermark and expected_watermark != watermark:
        return {**base, "reason": "conflict"}
    try:
        latest = latest_watermark(task_id)
    except Exception as exc:  # noqa: BLE001 读不到归档状态时保守放弃压缩
        return {**base, "reason": f"watermark_unavailable:{type(exc).__name__}"}
    if latest and latest != watermark:
        return {**base, "reason": "conflict"}

    # 2) 压缩前完整 transcript 加密归档
    archive_meta: dict[str, Any] | None = None
    if archive:
        try:
            archive_meta = archive_transcript(task_id, messages, reason="compaction")
        except Exception as exc:  # noqa: BLE001 归档失败不得静默压缩（无法回退）
            return {**base, "reason": f"archive_failed:{type(exc).__name__}"}

    try:
        # 3) 中间段 → 导航摘要（超大旧工具结果在此步转为内容引用）
        digest, replaced = _build_digest(state, messages, 1, boundary)
        compacted = [messages[0], {"role": "user", "content": digest}, *messages[boundary:]]
        after_chars = _messages_chars(compacted)
        if after_chars >= before_chars:
            # 越压越大：保留原文，不写回
            return {
                **base, "reason": "no_reduction", "archive": archive_meta,
                "stats": {**stats, "compactedMessages": 0, "slimmedToolResults": replaced},
            }
    except Exception as exc:  # noqa: BLE001 压缩失败回退原文
        return {**base, "reason": f"error:{type(exc).__name__}", "archive": archive_meta}

    stats.update({
        "after": after_chars, "compactedMessages": boundary - 1,
        "slimmedToolResults": replaced, "mode": "v2",
    })
    return {
        "messages": compacted, "stats": stats, "archive": archive_meta,
        "watermark": watermark, "applied": True, "reason": "compacted",
    }


def should_compact(messages: list[dict[str, Any]], threshold_chars: int) -> bool:
    return _messages_chars(messages) > threshold_chars


def stats_for_event(result: dict[str, Any]) -> dict[str, Any]:
    """压缩事件载荷：只含计数与归档标识（不含摘要正文）。"""
    stats = result.get("stats") or {}
    archive = result.get("archive") or {}
    return {
        "before": int(stats.get("before") or 0),
        "after": int(stats.get("after") or 0),
        "compactedMessages": int(stats.get("compactedMessages") or 0),
        "slimmedToolResults": int(stats.get("slimmedToolResults") or 0),
        "applied": bool(result.get("applied")),
        "reason": str(result.get("reason") or ""),
        "archiveId": archive.get("archiveId"),
    }
