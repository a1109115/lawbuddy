<!-- Maintained by Lu Lingyan, Deheng (Wuxi) Law Firm. -->
# [案件名称] 阅卷报告（被告立场）

> **分析日期**：{{REPORT_DATE}}
> **材料数量**：{{TOTAL_COUNT}} 份（起诉状 1 份 + 证据 {{EVIDENCE_COUNT}} 份）
> **分析立场**：被告 — 解构路径
> **OCR 引擎**：{{OCR_ENGINE}}

---

## 一、材料清单与 OCR 处理

### 1.1 程序文书

| 序号 | 编号 | 文件名 | 类型 | OCR 状态 |
|---|---|---|---|---|
| 1 | 起诉状 | {{COMPLAINT_FILENAME}} | 起诉状 | {{COMPLAINT_OCR_STATUS}} |

### 1.2 证据材料

{{EVIDENCE_LIST_TABLE}}

---

## 二、对方起诉状要点提取

### 2.1 基本信息

| 项目 | 内容 |
|---|---|
| 案由 | {{CAUSE_OF_ACTION}} |
| 原告 | {{PLAINTIFF}} |
| 被告 | {{DEFENDANT}} |
| 诉讼请求金额 | {{CLAIM_AMOUNT}} |

### 2.2 诉讼请求

{{CLAIMS_LIST}}

### 2.3 对方事实与理由摘要

{{FACTS_SUMMARY}}

---

## 三、诉状解构（2026-08-30 新增）

### 3.1 诉讼请求结构分析

{{CLAIMS_STRUCTURE_ANALYSIS}}

> 拆解要点：主请求与附属请求的依存关系（如本金与利息）；每项金额的计算依据及出处；诉讼费/保函费/律师费承担主张的依据。

### 3.2 诉状陈述与事实核对

{{COMPLAINT_FACT_CHECK}}

> 只记录客观核对结果：该陈述有/无对应证据；与证据 X 是否矛盾。不做三性评价（核心原则 7）。

---

## 四、案由构成要件拆解

起诉状确定的案由为：**{{CAUSE_OF_ACTION}}**

按案由知识库的【请求权基础规范】拆解为以下要件：

{{LEGAL_ELEMENT_BREAKDOWN}}

---

## 五、逐要件攻击分析

### 要件一：{{ELEMENT_1_NAME}}

| 攻击维度 | 分析 |
|---------|------|
| 对方证据 | {{ELEMENT_1_EVIDENCE}} |
| 证据自洽性 | {{ELEMENT_1_SELF_CONSISTENCY}} |
| 与己方证据矛盾 | {{ELEMENT_1_CONTRADICTION}} |
| 法律评价 | {{ELEMENT_1_LEGAL_EVAL}} |
| **攻击结论** | {{ELEMENT_1_CONCLUSION}} |

### 要件二：{{ELEMENT_2_NAME}}

| 攻击维度 | 分析 |
|---------|------|
| 对方证据 | {{ELEMENT_2_EVIDENCE}} |
| 证据自洽性 | {{ELEMENT_2_SELF_CONSISTENCY}} |
| 与己方证据矛盾 | {{ELEMENT_2_CONTRADICTION}} |
| 法律评价 | {{ELEMENT_2_LEGAL_EVAL}} |
| **攻击结论** | {{ELEMENT_2_CONCLUSION}} |

（逐要件重复上述分析）

---

## 六、证据矛盾点清单（可利用的矛盾）

### 6.1 对方证据内部矛盾

{{PLAINTIFF_INTERNAL_CONTRADICTIONS}}

### 6.2 对方证据与己方证据矛盾

{{CROSS_PARTY_CONTRADICTIONS}}

### 6.3 对方证据链断裂点

{{EVIDENCE_CHAIN_BREAKS}}

---

## 七、程序性检查（时效 + 管辖）

### 7.1 诉讼时效判断

| 检查项 | 分析 |
|--------|------|
| 质保金/款项到期日 | {{DUE_DATE}} |
| 最近一次催告日 | {{LAST_DEMAND_DATE}} |
| 诉讼时效届满日 | {{LIMITATION_DEADLINE}} |
| 对方起诉日 | {{FILING_DATE}} |
| **时效判断** | {{LIMITATION_JUDGMENT}} |

### 7.2 管辖核对

| 检查项 | 案卷情况 | 核对结论 | 法律依据 |
|--------|---------|---------|---------|
| 仲裁条款 | {{ARBITRATION_CLAUSE}} | {{ARBITRATION_CONCLUSION}} | {{ARBITRATION_LEGAL_BASIS}} |
| 专属管辖 | {{EXCLUSIVE_JURISDICTION}} | {{EXCLUSIVE_CONCLUSION}} | {{EXCLUSIVE_LEGAL_BASIS}} |
| 协议/法定管辖 | {{AGREED_JURISDICTION}} | {{JURISDICTION_CONCLUSION}} | {{JURISDICTION_LEGAL_BASIS}} |

> 管辖异议是否提起由委托人决定，本报告仅呈现核对结论。

---

## 八、质证意见（立场经用户逐证据确认）

{{CROSS_EXAMINATION_OPINIONS}}

> 仅对用户标记"不认可/部分认可"的证据生成反驳意见（法律逻辑 / 证据链条 / 合理推论三维度）；用户标记"认可"的证据列名即可，不生成意见。

---

## 九、答辩策略建议

### 9.1 可成立的要件（承认部分）

{{ADMITTED_ELEMENTS}}

### 9.2 存疑/可攻击的要件（抗辩重点）

{{DEFENSE_FOCUS}}

### 9.3 建议的抗辩策略

{{DEFENSE_STRATEGY}}

### 9.4 需补充的己方证据

{{DEFENSE_EVIDENCE_NEEDED}}

---

## 十、时间轴（如有）

![案件时间轴](timeline.png)

```mermaid
{{MERMAID_TIMELINE}}
```

---

*本报告由 case-file-review Skill 自动生成（被告立场），所有结论均标注证据来源。*
