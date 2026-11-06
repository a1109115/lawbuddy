"""案件内全文检索（DEV-DOC-008 / 技术方案 §8.3）。

- FTS5 trigram 索引 + BM25 排序（中文子串可命中）；
- 查询词不足 3 字符（trigram 最小长度）或无命中时回退 LIKE；
- **案件强制过滤**：case_id 为必填参数并直接写入 SQL 条件，
  调用方（含未来的模型工具）无法省略或覆盖（技术方案 §8.1，AC-SEC-003）；
- 命中项保留检索方式与分数，为后续混合召回（embedding）预留；
- embedding 语义召回在检索地基迭代之后接入，届时按
  document_version_id + block_id 去重合并。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .storage import get_db


class SearchError(RuntimeError):
    pass


def search_pages(
    case_id: str,
    query: str,
    limit: int = 20,
    extra_case_ids: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """在指定案件内检索页文本。case_id 不可省略（跨案召回为 0 的门禁基础）。

    extra_case_ids 仅由显式关联案件（文书生成只读共享）传入；调用方不能省略
    主案件，其他任务仍按单案件强制过滤。
    """
    if not case_id:
        raise SearchError("caseId is required")
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(50, limit))
    ids = _case_ids(case_id, extra_case_ids)
    fts = _fts_hits(ids, query, limit)
    if fts:
        return fts
    return _like_hits(ids, query, limit)


def search_pages_multi(
    case_id: str,
    terms: list[str] | tuple[str, ...],
    limit: int = 20,
    extra_case_ids: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """对多个检索词分别检索并按「文档 + 页码」合并去重（05 方案 §10.4 D5 查询拆解）。

    只做词级拆解与合并，不引入向量库或额外服务：每个词仍走同一套
    FTS/LIKE + 案件过滤；同一页被多个词命中时合并 matchedTerms，
    阅读成本按去重后的页数计，而不是各词命中数相加。
    """
    cleaned: list[str] = []
    for term in terms:
        norm = " ".join(str(term or "").split())
        if norm and norm not in cleaned:
            cleaned.append(norm)
    if not cleaned:
        return []
    if len(cleaned) == 1:
        return search_pages(case_id, cleaned[0], limit, extra_case_ids)
    limit = max(1, min(50, limit))
    ids = _case_ids(case_id, extra_case_ids)
    return merge_hits(
        [(term, _fts_hits(ids, term, limit) or _like_hits(ids, term, limit)) for term in cleaned],
        limit,
    )


def merge_hits(
    term_hits: list[tuple[str, list[dict[str, Any]]]], limit: int = 20,
) -> list[dict[str, Any]]:
    """按「文档 + 页码」合并多路命中，命中项带 matchedTerms（05 方案 §6.7）。

    排序规则（主查询优先 + 扩展保底席位）：
    - 第一路是主查询，其命中页按该路原排序置于前列——同义扩展词不会把原本
      第 1 位的目标页挤到后面（阅读成本不因拆解上升）；
    - 同时给"只有扩展词命中"的页保留至少 1 个尾部席位：多概念问句拆词后，
      任一单路通常覆盖不满，没有保底席位就会把其他概念的相关页全部挤掉。
    同一页被多个词命中只返回一条，阅读成本按去重后的页数计。
    """
    limit = max(1, min(50, limit))
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    ranks: dict[tuple[str, int], list[tuple[int, int]]] = {}
    for idx, (term, hits) in enumerate(term_hits):
        for rank, hit in enumerate(hits, start=1):
            key = (str(hit["documentId"]), int(hit["pageNo"]))
            ranks.setdefault(key, []).append((idx, rank))
            prev = merged.get(key)
            if prev is None:
                merged[key] = {**hit, "matchedTerms": [term]}
                continue
            if term not in prev["matchedTerms"]:
                prev["matchedTerms"].append(term)
            if float(hit.get("score") or 0) > float(prev.get("score") or 0):
                prev["score"] = hit["score"]
                prev["snippet"] = hit["snippet"]
                prev["matchType"] = hit["matchType"]

    def primary_rank(key: tuple[str, int]) -> int | None:
        return next((rank for idx, rank in ranks[key] if idx == 0), None)

    prim = sorted((k for k in merged if primary_rank(k) is not None),
                  key=lambda k: primary_rank(k) or 0)
    rest = sorted((k for k in merged if primary_rank(k) is None),
                  key=lambda k: (min(rank for _i, rank in ranks[k]),
                                 -len(merged[k].get("matchedTerms") or []),
                                 int(merged[k]["pageNo"])))
    if not rest:
        return [merged[k] for k in prim[:limit]]
    reserve = 1
    keep = max(1, limit - reserve)
    order = prim[:keep] + rest[:reserve]
    for key in prim[keep:] + rest[reserve:]:
        if len(order) >= limit:
            break
        order.append(key)
    return [merged[key] for key in order[:limit]]


def _case_ids(case_id: str, extra_case_ids: tuple[str, ...]) -> list[str]:
    return [case_id, *[cid for cid in extra_case_ids if cid and cid != case_id]]


def _fts_hits(ids: list[str], query: str, limit: int) -> list[dict[str, Any]]:
    """FTS5 trigram 短语匹配 + BM25（查询词 >= 3 字符）。"""
    if len(query) < 3:
        return []
    in_clause = ",".join("?" for _ in ids)
    phrase = '"' + query.replace('"', '""') + '"'
    try:
        rows = get_db().query(
            f"""
            SELECT document_id, document_title, version_id, page_no, text_quality,
                   snippet(pages_fts, 0, '【', '】', '…', 24) AS snip,
                   -bm25(pages_fts) AS score
            FROM pages_fts
            WHERE pages_fts MATCH ? AND case_id IN ({in_clause})
            ORDER BY score DESC, page_no ASC
            LIMIT ?
            """,
            (phrase, *ids, limit),
        )
    except sqlite3.OperationalError:
        return []
    return [_to_hit(r, "fts") for r in rows]


def _like_hits(ids: list[str], query: str, limit: int) -> list[dict[str, Any]]:
    """LIKE 兜底（短查询 / FTS 无命中），仍强制案件过滤。"""
    in_clause = ",".join("?" for _ in ids)
    rows = get_db().query(
        f"""
        SELECT document_id, document_title, version_id, page_no, text_quality, text_content
        FROM pages_fts
        WHERE case_id IN ({in_clause}) AND text_content LIKE ?
        ORDER BY page_no ASC
        LIMIT ?
        """,
        (*ids, f"%{query}%", limit),
    )
    return [_to_hit({**r, "snip": _like_snippet(r["text_content"], query), "score": 0.0}, "like")
            for r in rows]


def _to_hit(row: dict[str, Any], match_type: str) -> dict[str, Any]:
    return {
        "documentId": row["document_id"],
        "documentTitle": row["document_title"],
        "versionId": row["version_id"],
        "pageNo": row["page_no"],
        "quality": row["text_quality"],
        "snippet": row["snip"],
        "score": round(float(row["score"] or 0.0), 4),
        "matchType": match_type,
    }


def _like_snippet(text: str, query: str, width: int = 24) -> str:
    """LIKE 命中的上下文摘录（前后各约 width 字符）。"""
    idx = text.find(query)
    if idx < 0:
        return text[: width * 2] + ("…" if len(text) > width * 2 else "")
    start = max(0, idx - width)
    end = min(len(text), idx + len(query) + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}【{text[idx : idx + len(query)]}】{text[idx + len(query) : end]}{suffix}"


def index_page(
    conn: sqlite3.Connection,
    *,
    text: str,
    case_id: str,
    document_id: str,
    document_title: str,
    version_id: str,
    page_no: int,
    text_quality: str,
) -> None:
    """导入管线在页写入事务内同步更新 FTS 索引。"""
    if not text:
        return
    conn.execute(
        """
        INSERT INTO pages_fts(text_content, case_id, document_id, document_title, version_id, page_no, text_quality)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (text, case_id, document_id, document_title, version_id, page_no, text_quality),
    )
