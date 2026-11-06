#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
案由知识库分卷合并脚本（2026-08-30 新增，v2.15.0）
用途：878 个 causes/*.md 按"部分"合并为 10 个分卷 md（解决 SkillHub 单包 200 文件限制），
      生成含 file/anchor 的新版 toc_index.json，检索协议改为"查索引 → grep 锚点定位"。
用法：python3 merge_cause_wiki.py <wiki根目录> <输出目录>
      例：python3 merge_cause_wiki.py ~/.workbuddy/wiki-knowledge/民事案由指引_第三版 references/cause-of-action-wiki
注意：源知识库结构（causes/parts/concepts/synthesis/index.md/toc_index.json/DISCLAIMER.md）变更时需同步本脚本。
"""
import json, os, re, sys, shutil

def parse_num(fname):
    """从文件名提取案由编号：(完整编号, parent_num, num, is_sub)；支持中文序号（海商法释义条目如 '11-一_'）"""
    m = re.match(r'^(\d+)-(\d+)_', fname)
    if m:
        return f"{m.group(1)}-{m.group(2)}", m.group(1), m.group(2), True
    m = re.match(r'^(\d+)-([一二三四五六七八九十]+)_', fname)
    if m:
        return f"{m.group(1)}-{m.group(2)}", m.group(1), m.group(2), True
    m = re.match(r'^(\d+)_', fname)
    if m:
        return m.group(1), m.group(1), None, False
    return None, None, None, False

def natural_key(num):
    return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', num)]

def main():
    if len(sys.argv) != 3:
        print(__doc__); sys.exit(1)
    src_root, out_dir = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    wiki_dir = os.path.join(src_root, 'wiki')

    # 1. 读 toc
    toc = json.load(open(os.path.join(src_root, 'toc_index.json'), encoding='utf-8'))
    causes_idx = {c['num']: c for c in toc if c['type'] == 'cause'}
    subs_idx = {(c['parent_num'], c['num']): c for c in toc if c['type'] == 'subcause'}
    part_order = [c['name'] for c in toc if c['type'] == 'part']
    print(f'源: {len(causes_idx)} 三级案由 + {len(subs_idx)} 四级案由，{len(part_order)} 个部分')

    # 2. 遍历 causes 文件分组
    groups = {p: [] for p in part_order}
    unmatched = []
    for f in sorted(os.listdir(os.path.join(wiki_dir, 'causes'))):
        if not f.endswith('.md'):
            continue
        full, parent, num, is_sub = parse_num(f)
        if is_sub:
            info = subs_idx.get((parent, num))
        else:
            info = causes_idx.get(parent)
        if not info:
            unmatched.append(f); continue
        part = info['part']
        if part not in groups:
            groups[part] = []
        groups[part].append((full, info, f))

    print(f'匹配分组: {sum(len(v) for v in groups.values())}/878，未匹配: {len(unmatched)}')
    for f in unmatched[:5]:
        print('  未匹配:', f)

    # 3. 清理输出目录
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(os.path.join(out_dir, 'parts-merged'))
    for sub in ('concepts', 'synthesis'):
        shutil.copytree(os.path.join(wiki_dir, sub), os.path.join(out_dir, sub))
    shutil.copy(os.path.join(src_root, 'DISCLAIMER.md'), out_dir)

    # 4. 生成分卷 + 新 toc
    new_toc = [c for c in toc if c['type'] != 'cause' and c['type'] != 'subcause']
    total_blocks = 0
    for idx, part in enumerate(part_order, 1):
        items = sorted(groups[part], key=lambda x: natural_key(x[0]))
        fname = f'part-{idx:02d}_{part}.md'
        lines = [f'# {part}', '',
                 f'> 本分卷由案由知识库 causes/ 按部分合并生成（merge_cause_wiki.py），含 {len(items)} 个案由。',
                 f'> 检索：查 toc_index.json 获取案由的 file 与 anchor，再在本文件中 grep 锚点 `{items[0][0] if items else ""}` 定位段落。', '']
        for full, info, src_f in items:
            body = open(os.path.join(wiki_dir, 'causes', src_f), encoding='utf-8').read()
            # 去掉 frontmatter
            if body.startswith('---'):
                parts_ = body.split('---', 2)
                body = parts_[2] if len(parts_) == 3 else body
            # 去掉首个 H1 标题行、尾部"参见"块（wiki 内部导航，合并后失效）
            body = re.sub(r'^#\s+.*\n', '', body, count=1, flags=re.M)
            body = re.sub(r'\n## 参见\n[\s\S]*$', '\n', body)
            body = body.rstrip()
            typ = 'subcause' if 'parent_num' in info else 'cause'
            parent_note = ''
            if typ == 'subcause':
                pf = full.split('-')[0]
                parent_note = f' · 父案由编号 {pf}（正文见其父级分卷锚点 `## {pf}_`）'
            anchor = f'{full}_{info["name"]}'
            lines.append(f'## {anchor}')
            lines.append(f'> 溯源：{info["book"]} p{info["page"]} · 部分：{part}{parent_note}')
            lines.append(body)
            lines.append('')
            new_toc.append({'type': typ, 'part': part,
                            'section': info.get('section', ''),
                            'num': info['num'], 'parent_num': info.get('parent_num', ''),
                            'name': info['name'], 'page': info['page'], 'book': info['book'],
                            'file': f'parts-merged/{fname}', 'anchor': f'## {anchor}'})
            total_blocks += 1
        open(os.path.join(out_dir, 'parts-merged', fname), 'w', encoding='utf-8').write('\n'.join(lines))
        print(f'✅ {fname}: {len(items)} 个案由，{os.path.getsize(os.path.join(out_dir, "parts-merged", fname))//1024} KB')

    json.dump(new_toc, open(os.path.join(out_dir, 'toc_index.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=2)

    # 5. 重写 index.md（简版导航）
    idx_lines = ['---', 'title: 民事案件案由适用要点与请求权规范指引（第三版）索引（分卷版）', 'type: index', '---', '',
                 '# 案由知识库索引（分卷版）', '',
                 '> 878 个案由按部分合并为 10 个分卷（SkillHub 单包 200 文件限制）。检索：查 `toc_index.json` 的 file/anchor → grep 锚点定位段落。', '',
                 '## 分卷导航', '']
    for idx, part in enumerate(part_order, 1):
        idx_lines.append(f'- `parts-merged/part-{idx:02d}_{part}.md` — {part}')
    idx_lines += ['', '## 辅助', '', '- `concepts/` — 案由适用要点 / 请求权基础规范概念说明',
                  '- `synthesis/` — 民事案由体系概览 / 勘误与待确认',
                  '- `DISCLAIMER.md` — 版权免责声明', '']
    open(os.path.join(out_dir, 'index.md'), 'w', encoding='utf-8').write('\n'.join(idx_lines))

    # 6. 自检
    n_files = sum(len(fs) for _, _, fs in os.walk(out_dir))
    print(f'\n完成：{total_blocks} 个案由块 → {out_dir}（{n_files} 个文件）')
    assert total_blocks == 878, f'案由块数异常: {total_blocks}'
    assert n_files <= 200, f'文件数超限: {n_files}'
    print('自检通过：878 块完整，文件数 ≤200')

if __name__ == '__main__':
    main()
