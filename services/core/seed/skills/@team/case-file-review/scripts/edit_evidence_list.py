#!/usr/bin/env python3
"""
证据清单增量编辑脚本 — 插入/删除证据组并自动重编号（v2.6.0）。

核心原则：读完全量数据 → 在内存中增删改 → 重写干净 docx。
不做 XML 级别的局部修补（deepcopy + body.insert 极其脆弱）。

用法:
    # 查看现有分组
    python edit_evidence_list.py view <input.docx>

    # 在第 N 组之后插入新组（"after" 表示紧跟在第 N 组后面）
    python edit_evidence_list.py insert <input.docx> <output.docx> \
        --after 14 \
        --title "15、雨棚刷漆翻新改造工程" \
        --rows '[["15-1","四楼停车场雨棚刷漆翻新改造合同","证明...","15-1~15-16"]]'

    # 从 JSON 文件读取插入数据（推荐，避免 shell 转义问题）
    python edit_evidence_list.py insert <input.docx> <output.docx> \
        --after 14 --from-json insert_data.json

    # 删除第 N 组
    python edit_evidence_list.py delete <input.docx> <output.docx> --group 10

    # 原地修改（覆盖输入文件）
    python edit_evidence_list.py insert <input.docx> <input.docx> --after 14 --from-json data.json

JSON 格式（insert_data.json）:
    {
      "title": "15、雨棚刷漆翻新改造工程",
      "rows": [
        ["15-1", "施工合同", "证明…", "15-1~15-16"]
      ]
    }

依赖: python-docx (pip install python-docx)
作者: 陆凌燕（北京德恒（无锡）律师事务所）
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Pt, Cm, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
except ImportError:
    sys.exit("需要 python-docx: pip install python-docx")


# ═══════════════════════════════════════════════════════════════
# 数据提取
# ═══════════════════════════════════════════════════════════════

def _para_text(elem) -> str:
    """拼接段落中所有 w:t 文本（处理跨 run 拆分）"""
    return ''.join([t.text or '' for t in elem.findall('.//' + qn('w:t'))])


def _tbl_first_data_cell(elem) -> str:
    """表格第一数据行的第一个单元格文本"""
    rows = elem.findall('.//' + qn('w:tr'))
    if rows and len(rows) > 1:
        cells = rows[1].findall('.//' + qn('w:tc'))
        if cells:
            return ''.join([t.text or '' for t in cells[0].findall('.//' + qn('w:t'))])
    return ''


def _tbl_all_rows(elem) -> list[list[str]]:
    """提取表格所有数据行（跳过标题行）的内容"""
    rows = elem.findall('.//' + qn('w:tr'))
    result = []
    for row in rows[1:]:  # 跳过表头
        cells = row.findall('.//' + qn('w:tc'))
        row_data = [
            ''.join([t.text or '' for t in c.findall('.//' + qn('w:t'))])
            for c in cells
        ]
        result.append(row_data)
    return result


def extract_groups(doc_path: str) -> list[dict]:
    """从 docx 提取所有证据组的数据结构。

    Returns:
        [{"title": "1、车道雨棚...", "group_num": 1, "rows": [[...], ...]}, ...]
    """
    doc = Document(doc_path)
    body = doc.element.body
    elements = list(body)
    groups = []

    i = 0
    while i < len(elements):
        tag = _tag(elements[i])
        if tag == 'p':
            pt = _para_text(elements[i])
            if pt and pt[0].isdigit() and '、' in pt[:10]:
                j = i + 1
                while j < len(elements):
                    if _tag(elements[j]) == 'tbl':
                        first = _tbl_first_data_cell(elements[j])
                        m = re.match(r'^(\d+)-', first)
                        if m:
                            groups.append({
                                "title": pt,
                                "group_num": int(m.group(1)),
                                "rows": _tbl_all_rows(elements[j]),
                                "_title_elem": elements[i],
                                "_table_elem": elements[j],
                            })
                        break
                    j += 1
        i += 1
    return groups


# ═══════════════════════════════════════════════════════════════
# 数据操作
# ═══════════════════════════════════════════════════════════════

def insert_group(
    groups: list[dict],
    after_index: int,
    title: str,
    rows: list[list[str]],
) -> list[dict]:
    """在指定位置后插入新组，并重编号。

    Args:
        groups: 现有组列表
        after_index: 在此组的 group_num 之后插入（按 group_num 定位，不是列表索引）
        title: 新组的标题（如 "15、雨棚刷漆翻新改造工程"）
        rows: 新组的证据行，每行 [编号, 证据名称, 证明目的, 页码]

    Returns:
        修改后的 groups 列表（原地修改 + 返回引用）
    """
    # 找到插入位置（按 group_num 排序后的索引）
    sorted_groups = sorted(groups, key=lambda g: g["group_num"])
    insert_pos = None
    for idx, g in enumerate(sorted_groups):
        if g["group_num"] == after_index:
            insert_pos = idx + 1
            break

    if insert_pos is None:
        raise ValueError(f"未找到 group_num={after_index} 的组，无法确定插入位置")

    # 新组的 group_num = after_index + 1（但可能被占用，需要统一重编）
    new_group_num = after_index + 1

    # 规范化新组的行内容：将用户提供的任意组号替换为正确的 new_group_num
    normalized_rows = []
    for row in rows:
        new_row = list(row)
        for col_idx in [0, 3]:
            if col_idx < len(new_row):
                # 替换任何组号前缀为正确的组号
                new_row[col_idx] = re.sub(
                    r'\b\d+-', f'{new_group_num}-', new_row[col_idx]
                )
        normalized_rows.append(new_row)

    new_group = {
        "title": title if str(new_group_num) in title.split('、')[0]
                else re.sub(r'^\d+、', f'{new_group_num}、', title),
        "group_num": new_group_num,
        "rows": normalized_rows,
    }
    sorted_groups.insert(insert_pos, new_group)

    # 重编号：从插入位置之后的所有组，组号 +1
    for idx in range(insert_pos + 1, len(sorted_groups)):
        old_num = sorted_groups[idx]["group_num"]
        new_num = old_num + 1

        # 标题
        sorted_groups[idx]["title"] = re.sub(
            r'^\d+、', f'{new_num}、', sorted_groups[idx]["title"]
        )
        # 表格行中的组号前缀（第0列编号 + 第3列页码）
        for row in sorted_groups[idx]["rows"]:
            for col_idx in [0, 3]:
                if col_idx < len(row):
                    row[col_idx] = re.sub(
                        r'\b' + str(old_num) + r'-',
                        f'{new_num}-',
                        row[col_idx],
                    )

        sorted_groups[idx]["group_num"] = new_num

    return sorted_groups


def delete_group(groups: list[dict], group_num: int) -> list[dict]:
    """删除指定组号的组，并重编号后续组。

    Returns:
        修改后的 groups 列表
    """
    sorted_groups = sorted(groups, key=lambda g: g["group_num"])

    # 找到并删除
    del_idx = None
    for idx, g in enumerate(sorted_groups):
        if g["group_num"] == group_num:
            del_idx = idx
            break

    if del_idx is None:
        raise ValueError(f"未找到 group_num={group_num} 的组")

    sorted_groups.pop(del_idx)

    # 重编号后续组
    for idx in range(del_idx, len(sorted_groups)):
        old_num = sorted_groups[idx]["group_num"]
        new_num = old_num - 1

        sorted_groups[idx]["title"] = re.sub(
            r'^\d+、', f'{new_num}、', sorted_groups[idx]["title"]
        )
        for row in sorted_groups[idx]["rows"]:
            for col_idx in [0, 3]:
                if col_idx < len(row):
                    row[col_idx] = re.sub(
                        r'\b' + str(old_num) + r'-',
                        f'{new_num}-',
                        row[col_idx],
                    )
        sorted_groups[idx]["group_num"] = new_num

    return sorted_groups


# ═══════════════════════════════════════════════════════════════
# 重写 docx
# ═══════════════════════════════════════════════════════════════

def _tag(elem) -> str:
    return elem.tag.split('}')[1] if '}' in elem.tag else elem.tag


def _is_title_para(elem) -> bool:
    """判断是否为证据组标题段落（以数字+、开头）"""
    if _tag(elem) != 'p':
        return False
    text = _para_text(elem)
    return bool(text and text[0].isdigit() and '、' in text[:10])


def _rebuild_docx(template_path: str, output_path: str, groups: list[dict]):
    """基于模板 docx 重建证据清单，替换各组内容。

    保持模板的样式（字体、边距、页眉页脚等），只替换分组区域的内容。
    策略：找到模板中第一个标题段落到最后一个表格之间的区域，
    用新的分组数据重写这部分。
    """
    doc = Document(template_path)
    body = doc.element.body
    elements = list(body)

    # 找到证据清单的起止范围
    first_title_idx = None
    last_table_idx = None
    for i, elem in enumerate(elements):
        if first_title_idx is None and _is_title_para(elem):
            first_title_idx = i
        if _tag(elem) == 'tbl':
            # 检查是否是证据表格（第一列是数字编号）
            first = _tbl_first_data_cell(elem)
            if re.match(r'^\d+-\d+', first):
                last_table_idx = i

    if first_title_idx is None or last_table_idx is None:
        raise ValueError("无法识别证据清单结构（未找到标题段落或证据表格）")

    # 收集模板中的格式模板
    # 标题段落格式 → 从第一个标题复制
    title_template = deepcopy(elements[first_title_idx])
    # 表格格式 → 从第一个表格复制
    table_template = deepcopy(elements[first_title_idx + 1])
    # 空行格式 → 从标题和表格之间的空行复制
    for k in range(first_title_idx + 1, last_table_idx):
        if _tag(elements[k]) == 'p' and _para_text(elements[k]).strip() == '':
            empty_para_template = deepcopy(elements[k])
            break
    else:
        # 没找到空行，创建一个
        empty_para_template = deepcopy(title_template)
        for t in empty_para_template.findall('.//' + qn('w:t')):
            t.text = ''

    # 删除旧的分组区域（从第一个标题到最后一个表格，含中间的所有内容）
    # 先确定精确的删除范围（包括表格后的空行）
    delete_end = last_table_idx
    # 如果最后一个表格后面有空行，一并删除
    if delete_end + 1 < len(elements):
        if _tag(elements[delete_end + 1]) == 'p' and _para_text(elements[delete_end + 1]).strip() == '':
            delete_end += 1

    # 收集旧区域中的所有元素
    old_range = list(range(first_title_idx, delete_end + 1))
    for idx in sorted(old_range, reverse=True):
        body.remove(elements[idx])

    # 插入位置 = first_title_idx（旧的已删，新内容从这里开始）
    insert_at = first_title_idx

    # 构建新内容
    # 使用模板表格获取列数
    template_rows = table_template.findall('.//' + qn('w:tr'))
    num_cols = len(template_rows[0].findall('.//' + qn('w:tc'))) if template_rows else 4

    for g_idx, group in enumerate(groups):
        # 标题段落
        title_elem = deepcopy(title_template)
        _set_para_text(title_elem, group["title"])
        body.insert(insert_at, title_elem)
        insert_at += 1

        # 表格
        tbl_elem = _build_table_from_template(
            table_template, group["rows"], num_cols
        )
        body.insert(insert_at, tbl_elem)
        insert_at += 1

        # 空行分隔（最后一组不加空行）
        if g_idx < len(groups) - 1:
            sep = deepcopy(empty_para_template)
            body.insert(insert_at, sep)
            insert_at += 1

    doc.save(output_path)


def _set_para_text(elem, text: str):
    """设置段落文本（清空所有 w:t 后写入第一个）"""
    t_elems = elem.findall('.//' + qn('w:t'))
    for t in t_elems:
        t.text = ''
    if t_elems:
        t_elems[0].text = text


def _build_table_from_template(template_elem, rows_data: list[list[str]], num_cols: int):
    """从模板表格创建新表格，替换数据行内容"""
    tbl = deepcopy(template_elem)
    existing_rows = tbl.findall('.//' + qn('w:tr'))

    # 保留表头行（第0行），删除其余数据行
    header = existing_rows[0]
    for r in existing_rows[1:]:
        tbl.remove(r)

    # 添加数据行
    for row_data in rows_data:
        new_row = deepcopy(header)  # 复制表头行作为格式模板
        # 去掉加粗（表头加粗，正文不加粗）
        for rPr in new_row.findall('.//' + qn('w:rPr')):
            for bold_tag in [qn('w:b'), qn('w:bCs')]:
                b = rPr.find(bold_tag)
                if b is not None:
                    rPr.remove(b)
        cells = new_row.findall('.//' + qn('w:tc'))
        for ci, text in enumerate(row_data):
            if ci < len(cells):
                _set_cell_text(cells[ci], text)
        tbl.append(new_row)

    return tbl


def _set_cell_text(cell_elem, text: str):
    """设置单元格文本"""
    for t in cell_elem.findall('.//' + qn('w:t')):
        t.text = ''
    t_elems = cell_elem.findall('.//' + qn('w:t'))
    if t_elems:
        t_elems[0].text = text


# ═══════════════════════════════════════════════════════════════
# 验证
# ═══════════════════════════════════════════════════════════════

def verify(doc_path: str, expected_count: int | None = None) -> dict:
    """验证证据清单的完整性。

    Returns:
        {"groups": [...], "gaps": [...], "mismatches": [...], "empty_proofs": [...], "ok": bool}
    """
    groups = extract_groups(doc_path)
    result = {
        "groups": [(g["group_num"], g["title"][:50]) for g in groups],
        "gaps": [],
        "mismatches": [],
        "empty_proofs": [],
        "ok": True,
    }

    group_nums = [g["group_num"] for g in groups]

    # 检查缺口
    max_num = max(group_nums) if group_nums else 0
    for n in range(1, max_num + 1):
        if n not in group_nums:
            result["gaps"].append(n)
            result["ok"] = False

    # 检查标题与表格一致
    for g in groups:
        title_match = re.match(r'^(\d+)、', g["title"])
        if title_match:
            title_num = int(title_match.group(1))
            if title_num != g["group_num"]:
                result["mismatches"].append(
                    f'标题 {title_num} ≠ 表格 {g["group_num"]}: {g["title"][:40]}'
                )
                result["ok"] = False

    # 检查空白证明目的
    for g in groups:
        for row in g["rows"]:
            if len(row) >= 3 and (not row[2] or row[2].strip() == ''):
                result["empty_proofs"].append(
                    f'{row[0] if row else "?"}: 空白证明目的'
                )
                result["ok"] = False

    # 期望数量检查
    if expected_count is not None and len(groups) != expected_count:
        result["ok"] = False

    return result


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def cmd_view(args):
    """查看现有分组"""
    groups = extract_groups(args.input)
    print(f"共 {len(groups)} 组:\n")
    for g in groups:
        print(f"  [{g['group_num']:2d}] {g['title'][:60]}")
        for row in g["rows"]:
            eid = row[0] if row else ''
            name = row[1][:30] if len(row) > 1 else ''
            print(f"       {eid:<8s} {name}")
        print()


def cmd_insert(args):
    """插入新组"""
    # 备份（如果不是原地修改）
    if args.input == args.output:
        backup = args.input + '.bak'
        shutil.copy2(args.input, backup)
        print(f"已备份: {backup}")

    # 读取插入数据
    if args.from_json:
        with open(args.from_json, 'r', encoding='utf-8') as f:
            data = json.load(f)
        title = data["title"]
        rows = data["rows"]
    else:
        title = args.title
        rows = json.loads(args.rows)

    # 提取 → 插入 → 重写
    groups = extract_groups(args.input)
    print(f"原始: {len(groups)} 组")

    groups = insert_group(groups, args.after, title, rows)
    print(f"插入后: {len(groups)} 组")

    _rebuild_docx(args.input, args.output, groups)

    # 验证
    result = verify(args.output)
    if result["ok"]:
        print("✅ 验证通过")
    else:
        print("❌ 验证发现问题:")
        if result["gaps"]:
            print(f"   缺口: {result['gaps']}")
        if result["mismatches"]:
            for m in result["mismatches"]:
                print(f"   不匹配: {m}")
        if result["empty_proofs"]:
            for e in result["empty_proofs"]:
                print(f"   空白: {e}")

    print(f"已保存: {args.output}")


def cmd_delete(args):
    """删除组"""
    if args.input == args.output:
        backup = args.input + '.bak'
        shutil.copy2(args.input, backup)
        print(f"已备份: {backup}")

    groups = extract_groups(args.input)
    print(f"原始: {len(groups)} 组")

    groups = delete_group(groups, args.group)
    print(f"删除后: {len(groups)} 组")

    _rebuild_docx(args.input, args.output, groups)

    result = verify(args.output)
    if result["ok"]:
        print("✅ 验证通过")
    else:
        print("❌ 验证发现问题:")
        if result["gaps"]:
            print(f"   缺口: {result['gaps']}")
        if result["mismatches"]:
            for m in result["mismatches"]:
                print(f"   不匹配: {m}")

    print(f"已保存: {args.output}")


def main():
    parser = argparse.ArgumentParser(
        description="证据清单增量编辑 — 插入/删除证据组并自动重编号"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # view
    p_view = sub.add_parser("view", help="查看现有分组")
    p_view.add_argument("input", help="证据清单 .docx 文件")
    p_view.set_defaults(func=cmd_view)

    # insert
    p_ins = sub.add_parser("insert", help="插入新证据组")
    p_ins.add_argument("input", help="输入 .docx")
    p_ins.add_argument("output", help="输出 .docx（可与输入相同）")
    p_ins.add_argument("--after", type=int, required=True,
                       help="在 group_num=N 的组之后插入")
    p_ins.add_argument("--title", help="新组标题（如 '15、XX工程'）")
    p_ins.add_argument("--rows", help='证据行 JSON（如 \'[["15-1","合同","证明","15-1~15-16"]]\'）')
    p_ins.add_argument("--from-json", help="从 JSON 文件读取 title + rows")
    p_ins.set_defaults(func=cmd_insert)

    # delete
    p_del = sub.add_parser("delete", help="删除证据组")
    p_del.add_argument("input", help="输入 .docx")
    p_del.add_argument("output", help="输出 .docx（可与输入相同）")
    p_del.add_argument("--group", type=int, required=True,
                       help="要删除的 group_num")
    p_del.set_defaults(func=cmd_delete)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
