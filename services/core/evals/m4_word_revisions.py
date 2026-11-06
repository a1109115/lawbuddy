# -*- coding: utf-8 -*-
"""M4 评测器：Word 原生修订与批注（P1.3）。

用法（services/core 目录下）：
    python -m evals.m4_word_revisions

覆盖：
1. 插入/删除修订写入 DOCX 原生 w:ins / w:del（删除文本用 w:delText），
   批注写入 commentRangeStart/End、commentReference 与 word/comments.xml；
2. DOCX 读回修订与批注标记（作者/时间/批注正文），不再把修订视为不可编辑；
3. 纯文本视图按「已接受」结果取（不含待删文本）；
4. 接受/拒绝修订只增删对应文本，批注可单独清除；
5. 合同风险建议以删除修订 + 插入修订 + 风险批注写入模型（原文保留待接受）；
6. 非法修订字段被校验拒绝。

门槛：全部断言通过。
"""

from __future__ import annotations

import io
import zipfile

DATE = "2026-10-28T09:30:00Z"


def _model() -> dict:
    return {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "甲方应"},
                    {"type": "text", "text": "于2026年1月1日前", "marks": [{
                        "type": "deletion",
                        "attrs": {"author": "LawBuddy", "date": DATE, "revisionId": "rv-1"},
                    }]},
                    {"type": "text", "text": "在验收后十日内", "marks": [
                        {"type": "insertion", "attrs": {
                            "author": "LawBuddy", "date": DATE, "revisionId": "rv-1",
                        }},
                        {"type": "comment", "attrs": {
                            "author": "LawBuddy", "date": DATE, "revisionId": "c-rv-1",
                            "body": "风险：付款期限过早；来源：《购销合同》 · 第2页",
                        }},
                    ]},
                    {"type": "text", "text": "支付价款。"},
                ],
            },
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "第二段仍需核实", "marks": [{
                        "type": "comment",
                        "attrs": {"author": "张律师", "date": DATE,
                                   "revisionId": "c-2", "body": "请核对金额"},
                    }]},
                ],
            },
        ],
    }


def _texts(model: dict) -> list[str]:
    values: list[str] = []

    def walk(node: dict) -> None:
        for child in node.get("content") or []:
            if child.get("type") == "text":
                values.append(child.get("text") or "")
            else:
                walk(child)

    walk(model)
    return values


def _marks_of(model: dict, text: str) -> list[dict]:
    found: list[dict] = []

    def walk(node: dict) -> None:
        for child in node.get("content") or []:
            if child.get("type") == "text":
                if child.get("text") == text:
                    found.extend(child.get("marks") or [])
            else:
                walk(child)

    walk(model)
    return found


def main() -> int:
    from lawbuddy import word

    model = _model()
    word.validate_model(model)
    data = word.to_docx(model)

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        document = archive.read("word/document.xml").decode("utf-8")
        comments = archive.read("word/comments.xml").decode("utf-8")
        content_types = archive.read("[Content_Types].xml").decode("utf-8")
        rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")

    assert "<w:ins" in document and "<w:del" in document, "修订未写入原生 DOCX"
    assert "<w:delText" in document and "于2026年1月1日前" in document, "删除文本未保留为 delText"
    assert "<w:commentRangeStart" in document and "<w:commentRangeEnd" in document
    assert "<w:commentReference" in document
    assert "word/comments.xml" in names
    assert 'PartName="/word/comments.xml"' in content_types
    assert "relationships/comments" in rels
    assert "风险：付款期限过早" in comments and "请核对金额" in comments
    assert 'w:author="LawBuddy"' in document
    print("[PASS] 插入/删除修订与批注写入原生 DOCX 部件")

    back, warnings = word.from_docx(data, simplify=False)
    assert not warnings, warnings
    assert "deletion" in {mark["type"] for mark in _marks_of(back, "于2026年1月1日前")}
    inserted_marks = _marks_of(back, "在验收后十日内")
    assert "insertion" in {mark["type"] for mark in inserted_marks}
    comment_mark = next(mark for mark in inserted_marks if mark["type"] == "comment")
    assert "风险：付款期限过早" in (comment_mark.get("attrs") or {}).get("body", "")
    assert (comment_mark.get("attrs") or {}).get("author") == "LawBuddy"
    second_comment = next(mark for mark in _marks_of(back, "第二段仍需核实") if mark["type"] == "comment")
    assert (second_comment.get("attrs") or {}).get("body") == "请核对金额"
    print("[PASS] DOCX 读回修订与批注（作者/正文）")

    plain = word.model_text(model)
    assert "于2026年1月1日前" not in plain, plain
    assert "在验收后十日内" in plain and "支付价款" in plain, plain
    print("[PASS] 纯文本视图按接受后结果取（不含待删文本）")

    accepted = word.resolve_revisions(model, "accept")
    accepted_texts = _texts(accepted)
    assert "于2026年1月1日前" not in accepted_texts, accepted_texts
    assert "在验收后十日内" in accepted_texts
    assert "insertion" not in {m["type"] for m in _marks_of(accepted, "在验收后十日内")}
    assert "deletion" not in {m["type"] for m in _marks_of(accepted, "在验收后十日内")}
    print("[PASS] 接受修订：删除原文、保留新增文本")

    rejected = word.resolve_revisions(model, "reject")
    rejected_texts = _texts(rejected)
    assert "在验收后十日内" not in rejected_texts, rejected_texts
    assert "于2026年1月1日前" in rejected_texts
    assert "deletion" not in {m["type"] for m in _marks_of(rejected, "于2026年1月1日前")}
    print("[PASS] 拒绝修订：保留原文、移除新增文本")

    cleared = word.resolve_comments(model)
    assert "comment" not in {m["type"] for m in _marks_of(cleared, "第二段仍需核实")}
    assert "第二段仍需核实" in _texts(cleared)
    targeted = word.resolve_comments(model, ["c-2"])
    assert "comment" not in {m["type"] for m in _marks_of(targeted, "第二段仍需核实")}
    assert "comment" in {m["type"] for m in _marks_of(targeted, "在验收后十日内")}
    print("[PASS] 批注清除（全部/指定）")

    contract_model = {"type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "合同价款为十万元。"},
    ]}]}
    word._attach_revision_marks(  # noqa: SLF001
        contract_model,
        "合同价款为十万元。",
        "合同价款以双方确认表为准。",
        "LawBuddy", DATE, "rv-risk-1",
        "风险：价款不确定；来源：《购销合同》 · 第1页",
    )
    original_marks = _marks_of(contract_model, "合同价款为十万元。")
    assert "deletion" in {m["type"] for m in original_marks}, original_marks
    replacement_marks = _marks_of(contract_model, "合同价款以双方确认表为准。")
    assert "insertion" in {m["type"] for m in replacement_marks}
    suggestion_comment = next(m for m in replacement_marks if m["type"] == "comment")
    assert "风险：价款不确定" in (suggestion_comment.get("attrs") or {}).get("body", "")
    assert word.model_text(contract_model).strip() == "合同价款以双方确认表为准。"
    print("[PASS] 合同风险建议落为删除+插入修订与批注（原文保留待接受）")

    bad = _model()
    bad["content"][0]["content"][1]["marks"][0]["attrs"]["fontSize"] = "12pt"
    try:
        word.validate_model(bad)
    except word.WordError as exc:
        assert "修" in str(exc) or "批注" in str(exc)
    else:
        raise AssertionError("修订标记携带文本样式应被拒绝")
    print("[PASS] 非法修订字段被校验拒绝")

    print("M4 Word 原生修订与批注：PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
