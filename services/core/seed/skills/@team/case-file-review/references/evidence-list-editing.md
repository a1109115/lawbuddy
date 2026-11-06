# 证据清单增量编辑 — 踩坑记录与实现参考

> 2026-08-11 建立，基于"三安建设诉尚正管理"案第15组补入的实际操作经验。

## 踩坑清单（按严重程度排列）

### 🔴 P0：绝不直接 XML 插入

**错误做法**：用 `deepcopy(old_table_elem)` + `body.insert(pos, new_elem)` 插入新表格。

**后果**：
1. `deepcopy` 的表格可能携带原表格的残留数据（行数不对、内容残留）
2. 插入后后续元素的索引全变，后续重编号的依据（位置/索引）不可靠
3. 两次脚本叠加后产生不可逆的级联损坏（V1 插入 → V2 再插入 → 双重编号偏移）

**正确做法**：读出全量数据 → 在 Python 数据结构中操作 → 用 python-docx 从头重写 docx。

### 🔴 P0：标题号与表组号是两个独立体系

**现象**：原文档中标题显示"10、江阴印象汇外围柏油路面"，但对应表格第一行是 `11-1`。标题号 ≠ 表组号，因为历史上第10组被删除后标题重编了但表未重编。

**后果**：如果用"标题号14之后插入"的逻辑，实际插入位置可能不对；如果用"表组号15之后插入"，标题又对不上。

**正确做法**：
1. 先完整提取标题-表格配对关系
2. 以**表组号**为准确定插入位置（表组号是证据编号的一部分，不能乱）
3. 插入后同时修正标题号使其与表组号对齐

### 🟡 P1：w:t 元素文本跨 run 拆分

**现象**：Word 文档中 "15、2020年零星改造工程" 可能被拆成：
```xml
<w:r><w:t>15</w:t></w:r>
<w:r><w:t>、2020年零星改造工程</w:t></w:r>
```

**后果**：`t.text.startswith('15、')` 永远匹配不到，标题修正静默失败。

**正确做法**：
```python
def para_text(elem):
    return ''.join([t.text or '' for t in elem.findall('.//' + qn('w:t'))])
# 然后对完整文本做正则匹配
```

### 🟡 P1：重编号从后往前

**错误做法**：从低组号往高组号改，如 15→16 先做，然后 16→17。

**后果**：15→16 之后，原来的 16 变成了 16，然后 16→17 会把已经改过的也再改一次，形成级联覆盖（15→16→17→18）。

**正确做法**：从高组号往低组号逐一改：
```python
for old_g in sorted(to_fix.keys(), reverse=True):
    new_g = to_fix[old_g]
    # 替换所有 old_g → new_g
```

### 🟢 P2：页码仅改组前缀

页码格式 `组号-页号`（如 `15-1~15-16`），重编号时只改组号前缀，不改页号（组内页号不变）。

## 推荐实现方案

### 方案：读完→改数据→重写

```python
from docx import Document
from docx.oxml.ns import qn
from copy import deepcopy
import shutil

# Step 0: 备份
shutil.copy2(original_path, original_path + '.bak')

# Step 1: 读取全量数据
doc = Document(original_path)
groups = extract_all_groups(doc)  # 返回 list[dict]

# Step 2: 在数据中插入
new_group = {
    "title": "15、雨棚刷漆翻新改造工程",
    "group_num": 15,
    "rows": [
        ["15-1", "四楼停车场雨棚刷漆翻新改造合同", 
         "证明原、被告签订施工合同……", "15-1~15-16"]
    ]
}
groups.insert(insert_index, new_group)  # 在第14组后（index 14）

# Step 3: 重编号
for i in range(insert_index + 1, len(groups)):
    old_num = groups[i]["group_num"]
    new_num = old_num + 1
    groups[i]["group_num"] = new_num
    # 修正标题
    groups[i]["title"] = re.sub(r'^\d+、', f'{new_num}、', groups[i]["title"])
    # 修正行内容中的组号前缀
    for row in groups[i]["rows"]:
        for j in [0, 3]:  # 编号列和页码列
            row[j] = re.sub(r'\b' + str(old_num) + r'-', f'{new_num}-', row[j])

# Step 4: 重写 docx
# 复制原文档样式作为模板
template_doc = Document(original_path + '.bak')
new_doc = rebuild_from_groups(groups, template_doc)
new_doc.save(original_path)

# Step 5: 验证
verify(new_doc, groups)
```

### 关键函数

```python
def extract_all_groups(doc):
    """提取所有组的标题和表格内容"""
    body = doc.element.body
    elements = list(body)
    groups = []
    
    def para_text(elem):
        return ''.join([t.text or '' for t in elem.findall('.//' + qn('w:t'))])
    
    def tbl_first_data_cell(elem):
        rows = elem.findall('.//' + qn('w:tr'))
        if rows and len(rows) > 1:
            cells = rows[1].findall('.//' + qn('w:tc'))
            if cells:
                return ''.join([t.text or '' for t in cells[0].findall('.//' + qn('w:t'))])
        return ''
    
    i = 0
    while i < len(elements):
        if elements[i].tag.endswith('p'):
            pt = para_text(elements[i])
            if pt and pt[0].isdigit() and '、' in pt:
                # 找下一个表格
                j = i + 1
                while j < len(elements):
                    if elements[j].tag.endswith('tbl'):
                        first_cell = tbl_first_data_cell(elements[j])
                        m = re.match(r'^(\d+)-', first_cell)
                        if m:
                            group_num = int(m.group(1))
                            rows_data = []
                            for row in elements[j].findall('.//' + qn('w:tr'))[1:]:
                                cells = row.findall('.//' + qn('w:tc'))
                                row_data = [''.join([t.text or '' for t in c.findall('.//' + qn('w:t'))]) for c in cells]
                                rows_data.append(row_data)
                            groups.append({
                                "title": pt,
                                "group_num": group_num,
                                "rows": rows_data
                            })
                        break
                    j += 1
        i += 1
    return groups
```

## 本次操作的实战数据

- 案件：三安建设诉尚正管理 建设工程施工合同纠纷
- 操作：在证据清单第14组和第15组之间插入第15组（雨棚刷漆翻新改造工程）
- 原分组数：25组（1-26，缺第10组）
- 插入后：25组（1-26，缺第10组，第15组为新增）
- 新增证据：1条（施工合同，16页）
- 脚本迭代次数：6次才成功（V1失败 → V2失败 → 逐组打补丁）

**教训**：如果一开始就用「读完→改数据→重写」方案，1次就能成功，不会产生27次 XML 级别的脏修改。
