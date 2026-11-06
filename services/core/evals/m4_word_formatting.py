# -*- coding: utf-8 -*-
"""M4 评测器：Word 文本格式（删除线 / 文字颜色 / 背景高亮 / 字体字号 / 段落排版）往返保真。

用法（services/core 目录下）：
    python -m evals.m4_word_formatting

覆盖：
1. 删除线写入 DOCX 原生 w:strike，并能读回为 strike mark；
2. 文字颜色写入 w:color，背景高亮写入 w:shd，读回为 textStyle color/backgroundColor；
3. 新增的本地字体（仿宋/楷体/黑体）与字号（10.5pt 等）可保存并读回；
4. 段落级首行缩进 / 段前段后 / 固定行距与 eastAsia 中文字体可往返，保证预览与编辑态排版一致；
5. 非法颜色值被校验拒绝（不允许把任意字符串写进 DOCX）。

门槛：全部断言通过。
"""

from __future__ import annotations

import io
import zipfile


def main() -> int:
    from lawbuddy import word

    model = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "attrs": {"textAlign": "justify"},
                "content": [
                    {"type": "text", "text": "加粗", "marks": [{"type": "bold"}]},
                    {"type": "text", "text": "删除", "marks": [{"type": "strike"}]},
                    {
                        "type": "text",
                        "text": "红色底黄",
                        "marks": [
                            {
                                "type": "textStyle",
                                "attrs": {
                                    "color": "#FF0000",
                                    "backgroundColor": "#FFFF00",
                                    "fontFamily": "FangSong",
                                    "fontSize": "10.5pt",
                                },
                            }
                        ],
                    },
                ],
            }
        ],
    }

    data = word.to_docx(model)
    xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8")
    assert "<w:strike" in xml, "删除线未写入 DOCX"
    assert "FF0000" in xml, "文字颜色未写入 DOCX"
    assert "FFFF00" in xml, "背景高亮未写入 DOCX"
    assert "FangSong" in xml and "21" in xml, "字体/字号未写入 DOCX"
    print("[PASS] 删除线 / 文字颜色 / 背景高亮 / 字体字号写入原生 DOCX")

    back, _warnings = word.from_docx(data, simplify=True)
    runs = back["content"][0]["content"]
    strike_run = next(n for n in runs if n.get("text") == "删除")
    strike_marks = {m["type"] for m in strike_run.get("marks", [])}
    assert "strike" in strike_marks, f"删除线未读回：{strike_run}"
    styled_run = next(n for n in runs if n.get("text") == "红色底黄")
    styled = next(m for m in styled_run["marks"] if m["type"] == "textStyle")["attrs"]
    assert styled.get("color") == "#FF0000", styled
    assert styled.get("backgroundColor") == "#ffff00", styled
    assert styled.get("fontFamily") == "FangSong", styled
    assert styled.get("fontSize") == "10.5pt", styled
    print("[PASS] 格式从 DOCX 读回模型（删除线/颜色/高亮/字体/字号）")

    # 段落级排版（首行缩进 / 段前段后 / 固定行距）+ eastAsia 中文字体：保证预览与编辑态一致
    layout_model = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "attrs": {
                    "textAlign": "justify",
                    "firstLineIndent": 32,
                    "spaceBefore": 6,
                    "spaceAfter": 4,
                    "lineSpacing": 30,
                },
                "content": [
                    {
                        "type": "text",
                        "text": "段落排版",
                        "marks": [
                            {"type": "textStyle", "attrs": {"fontFamily": "FangSong", "fontSize": "16pt"}}
                        ],
                    }
                ],
            }
        ],
    }
    layout_docx = word.to_docx(layout_model)
    layout_xml = zipfile.ZipFile(io.BytesIO(layout_docx)).read("word/document.xml").decode("utf-8")
    assert '<w:ind w:firstLine="640"/>' in layout_xml, "首行缩进未写入 w:ind"
    assert '<w:spacing w:before="120" w:after="80" w:line="600" w:lineRule="exact"/>' in layout_xml, \
        "段前段后/固定行距未写入 w:spacing"
    assert 'w:eastAsia="FangSong"' in layout_xml, "中文字体（eastAsia）未写入"
    layout_back, _warnings = word.from_docx(layout_docx, simplify=True)
    assert layout_back["content"][0]["attrs"] == {
        "textAlign": "justify",
        "firstLineIndent": 32.0,
        "spaceBefore": 6.0,
        "spaceAfter": 4.0,
        "lineSpacing": 30.0,
    }, layout_back["content"][0]["attrs"]
    print("[PASS] 段落排版（缩进/段间距/行距/eastAsia 字体）往返保真")

    try:
        word.validate_model(
            {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "x", "marks": [{"type": "textStyle", "attrs": {"color": "red"}}]}
                        ],
                    }
                ],
            }
        )
    except word.WordError as exc:
        assert "颜色" in str(exc)
    else:
        raise AssertionError("非法颜色应被拒绝")
    print("[PASS] 非法颜色值被校验拒绝")

    print("M4 Word 文本格式往返：PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
