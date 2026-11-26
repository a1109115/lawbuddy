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

import re
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
    document_ids: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """在指定案件内检索页文本。case_id 不可省略（跨案召回为 0 的门禁基础）。

    extra_case_ids 仅由显式关联案件（文书生成只读共享）传入；调用方不能省略
    主案件，其他任务仍按单案件强制过滤。document_ids 用于把检索限定在指定
    材料内——多份合同时「先定位是哪一份」比「在某一份里找第几页」更需要它。
    """
    if not case_id:
        raise SearchError("caseId is required")
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(50, limit))
    ids = _case_ids(case_id, extra_case_ids)
    docs = tuple(document_ids)
    fts = _fts_hits(ids, query, limit, docs)
    if fts:
        return _cover_documents(ids, query, limit, docs, fts)
    return _like_hits(ids, query, limit, docs)


def search_pages_multi(
    case_id: str,
    terms: list[str] | tuple[str, ...],
    limit: int = 20,
    extra_case_ids: tuple[str, ...] = (),
    document_ids: tuple[str, ...] = (),
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
        return search_pages(case_id, cleaned[0], limit, extra_case_ids, document_ids)
    limit = max(1, min(50, limit))
    ids = _case_ids(case_id, extra_case_ids)
    docs = tuple(document_ids)
    return merge_hits(
        [(term, _fts_hits(ids, term, limit, docs) or _like_hits(ids, term, limit, docs))
         for term in cleaned],
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


def _cover_documents(
    ids: list[str], query: str, limit: int,
    document_ids: tuple[str, ...], top: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """给「有多份材料同时命中」的检索保留每份材料的至少一个席位。

    全局 BM25 会按「页」排序：单份文档里的高分页会把其他材料的命中全部挤到
    limit 之外。卷宗里同一模板条款有十几份副本时，前 N 条常常只覆盖少数几份，
    使用者会误判「其余材料没有该条款」。这里在结果不满 limit、且存在未覆盖的
    命中文档时，按文档补一轮该文档内的最佳命中，直到填满。

    B-02 §4.7：全局 Top-N 已满时同样要为**零覆盖**文档保留席位——否则多文档
    任务里整份材料可以一次都不出现（HAR-T-004）。补席位只替换尾部条目
    （最多替掉 1/3 的 limit），主查询的头部排序保持不变。
    """
    if document_ids:
        return top
    covered = {str(h["documentId"]) for h in top}
    if len(covered) <= 1 and len(top) >= limit:
        return top
    missing = [d for d in _matching_documents(ids, query) if d not in covered]
    if not missing:
        return top
    extra: list[dict[str, Any]] = []
    budget = limit - len(top)
    if budget <= 0:
        # 结果已满：只为未覆盖文档挤出席位，且不挤掉超过 1/3 的全局结果
        budget = min(len(missing), max(1, limit // 3))
    for doc_id in missing:
        if len(extra) >= budget:
            break
        best = _fts_hits(ids, query, 1, (doc_id,))
        if best:
            extra.append(best[0])
    if not extra:
        return top
    if len(top) + len(extra) <= limit:
        return (top + extra)[:limit]
    keep = max(0, limit - len(extra))
    return (top[:keep] + extra)[:limit]


def _matching_documents(ids: list[str], query: str) -> list[str]:
    """列出该检索词命中的所有文档（按全局最佳分排序），用于补齐文档覆盖。

    与排序取前 N 页不同，这里问的是「哪些材料提到过」：同一模板条款在十几份
    副本里都有，按页截断会先耗尽 limit，导致其余材料看起来「没有」。
    """
    in_clause = ",".join("?" for _ in ids)
    phrase = ('"' + query.replace('"', '""') + '"') if len(query) >= 3 else ""
    tokens = [t for t in _tokenize(query) if len(t) >= 3]
    token_expr = " AND ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in tokens)
    for match in (phrase, token_expr):
        if not match or (match is token_expr and len(tokens) < 2):
            continue
        try:
            # bm25 是 FTS5 的辅助函数，不能在 GROUP BY 聚合里调用；这里只需要
            # 「哪些文档命中过」，命中次数足以给出稳定排序。
            rows = get_db().query(
                f"""
                SELECT document_id, COUNT(*) AS n
                FROM pages_fts
                WHERE pages_fts MATCH ? AND case_id IN ({in_clause})
                GROUP BY document_id
                ORDER BY n DESC, document_id ASC
                """,
                (match, *ids),
            )
        except sqlite3.OperationalError:
            continue
        if rows:
            return [str(r["document_id"]) for r in rows]
    return []


def _fts_query(
    ids: list[str], match: str, limit: int, document_ids: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """执行一次 FTS5 MATCH（match 为已构造好的表达式）。"""
    in_clause = ",".join("?" for _ in ids)
    doc_clause = ""
    doc_params: tuple[str, ...] = ()
    if document_ids:
        doc_clause = " AND document_id IN (" + ",".join("?" for _ in document_ids) + ")"
        doc_params = tuple(document_ids)
    try:
        rows = get_db().query(
            f"""
            SELECT document_id, document_title, version_id, page_no, text_quality,
                   snippet(pages_fts, 0, '【', '】', '…', 24) AS snip,
                   -bm25(pages_fts) AS score
            FROM pages_fts
            WHERE pages_fts MATCH ? AND case_id IN ({in_clause}){doc_clause}
            ORDER BY score DESC, page_no ASC
            LIMIT ?
            """,
            (match, *ids, *doc_params, limit),
        )
    except sqlite3.OperationalError:
        return []
    return [_to_hit(r, "fts") for r in rows]


def _phrase_expr(query: str) -> str:
    """整串短语表达式（原文逐字相邻才命中）。"""
    return '"' + query.replace('"', '""') + '"'


def _token_expr(query: str) -> str:
    """分词 AND 表达式：各词同页出现即可，不要求逐字相邻。"""
    tokens = [t for t in _tokenize(query) if len(t) >= 3]
    if len(tokens) < 2:
        return ""
    return " AND ".join(_phrase_expr(t) for t in tokens)


def _fts_hits(
    ids: list[str], query: str, limit: int, document_ids: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """FTS5 trigram 匹配 + BM25（查询词 >= 3 字符）。

    先按整串**短语**匹配（精度最高）；短语无命中时退回**分词 AND**匹配。
    原文常带弯引号、破折号或复数形式（`“MAE” means`、`acts of God`），
    整串短语会因一个标点或语序差异全部落空，而分词后同页出现即可命中——
    这正是用户在自然写法下遇到「明明有却搜不到」的主因。
    """
    if len(query) < 3:
        return []
    found = _fts_query(ids, _phrase_expr(query), limit, document_ids)
    if found:
        return found
    tokens = _token_expr(query)
    if not tokens:
        return []
    return _fts_query(ids, tokens, limit, document_ids)


def _tokenize(query: str) -> list[str]:
    """把查询拆成检索 token：按空白与常见标点切分，去掉纯标点片段。"""
    out: list[str] = []
    for raw in re.split(r"[\s,;:\u2018\u2019\u201c\u201d\"'“”‘’()\[\]{}<>/\\|]+", query or ""):
        token = raw.strip(".-—_")
        if token and any(ch.isalnum() for ch in token):
            out.append(token)
    return out


def _like_hits(
    ids: list[str], query: str, limit: int, document_ids: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """LIKE 兜底（短查询 / FTS 无命中），仍强制案件过滤。"""
    in_clause = ",".join("?" for _ in ids)
    doc_clause = ""
    doc_params: tuple[str, ...] = ()
    if document_ids:
        doc_clause = " AND document_id IN (" + ",".join("?" for _ in document_ids) + ")"
        doc_params = tuple(document_ids)
    rows = get_db().query(
        f"""
        SELECT document_id, document_title, version_id, page_no, text_quality, text_content
        FROM pages_fts
        WHERE case_id IN ({in_clause}) AND text_content LIKE ?{doc_clause}
        ORDER BY page_no ASC
        LIMIT ?
        """,
        (*ids, f"%{query}%", *doc_params, limit),
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
