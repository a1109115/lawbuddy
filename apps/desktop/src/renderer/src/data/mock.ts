/**
 * 演示数据：
 * - MODES：三模式配置（功能卡与建议问题）
 * - FALLBACK_FILES：仅浏览器调试模式（无 Electron 桥）使用的卷宗数据；
 *   Electron 运行时卷宗一律来自 Python Core 的真实导入管线，
 *   AI 产物来自 artifacts 表（M2 收尾，data 源 store.artifacts）。
 */

export type ModeKey = 'yj' | 'ht' | 'ws'
export type DocType = 'docx' | 'pdf' | 'xlsx' | 'txt' | 'md' | 'img'

/** 法律阅卷工作流预设（PRD 13 四类功能入口，对应 Core 的 workflowPreset） */
export type PresetKey = 'review_analysis' | 'visualization' | 'evidence_analysis' | 'strategy'

export const YJ_PRESETS: Array<{ key: PresetKey; ic: string; name: string; q: string }> = [
  {
    key: 'review_analysis',
    ic: 'i-doc',
    name: '阅卷分析',
    q: '请对本案卷宗做完整阅卷分析：卷宗清单与缺失材料、当事人关系、时间线、争议焦点、事实与证据对应、矛盾点，以及下一步工作。'
  },
  {
    key: 'visualization',
    ic: 'i-chart',
    name: '可视化阅卷',
    q: '请为本案生成时间轴、主体关系图，以及资金或证据流转图（材料确有流向时）；每个节点标注《文档名》与页码，无法确认的关联用虚线标待核实。'
  },
  {
    key: 'evidence_analysis',
    ic: 'i-search',
    name: '证据分析',
    q: '请逐份做证据卡分析（证明目的、三性、证明力、瑕疵），按争议焦点整理证据链与缺口，并给出质证提纲和补强调证建议。'
  },
  {
    key: 'strategy',
    ic: 'i-bulb',
    name: '辩护策略分析',
    q: '请站在我方立场生成本案辩护策略：列出有利与不利条件、对方可能主张，提出至少两套可行方案并比较前提、风险与反制，单列需要我决定的事项。'
  }
]

export interface ModeCfg {
  name: string
  sub: string
  features: Array<{ ic: string; name: string; desc: string; q: string }>
  suggests: string[]
}

export const MODES: Record<ModeKey, ModeCfg> = {
  yj: {
    name: '法律阅卷',
    sub: '专注法律场景的 AI 助手，助你高效阅卷、精准分析、专业创作',
    features: [
      {
        ic: 'i-doc',
        name: '阅卷分析',
        desc: '卷宗地图与争点',
        q: '请对本案卷宗做完整阅卷分析：卷宗清单与缺失材料、当事人关系、时间线、争议焦点、事实与证据对应、矛盾点，以及下一步工作。'
      },
      {
        ic: 'i-chart',
        name: '可视化阅卷',
        desc: '时间线·关系图',
        q: '请为本案生成时间轴、主体关系图，以及资金或证据流转图（材料确有流向时）；每个节点标注《文档名》与页码，无法确认的关联用虚线标待核实。'
      },
      {
        ic: 'i-search',
        name: '证据分析',
        desc: '证据卡与质证',
        q: '请逐份做证据卡分析（证明目的、三性、证明力、瑕疵），按争议焦点整理证据链与缺口，并给出质证提纲和补强调证建议。'
      },
      {
        ic: 'i-bulb',
        name: '辩护策略分析',
        desc: '方案比较',
        q: '请站在我方立场生成本案辩护策略：列出有利与不利条件、对方可能主张，提出至少两套可行方案并比较前提、风险与反制，单列需要我决定的事项。'
      }
    ],
    suggests: [
      '请梳理本案卷宗，先告诉我缺哪些关键材料，以及这对举证有什么影响。',
      '本案各争议焦点的举证责任分别由谁承担？现有证据是否足以支撑？',
      '按时间顺序梳理本案关键节点，标出对我方不利的节点和对应原文页码。'
    ]
  },
  ht: {
    name: '合同审查',
    sub: '上传合同即可逐条审查，风险定位、条款比对与修订建议一键完成',
    features: [
      { ic: 'i-shield', name: '风险识别', desc: '定位高风险条款', q: '请审查这份合同的主要风险点。' },
      { ic: 'i-link', name: '条款比对', desc: '差异逐项标注', q: '请比对两版合同的条款差异。' },
      { ic: 'i-calendar', name: '履约梳理', desc: '关键节点提醒', q: '请梳理合同履行的关键时间节点。' },
      { ic: 'i-edit', name: '修订建议', desc: '生成修改草稿', q: '请给出违约责任条款的修改建议。' }
    ],
    suggests: ['请审查这份合同的主要风险点。', '双方权利义务是否存在明显不对等？', '请给出违约责任条款的修改建议。']
  },
  ws: {
    name: '文书生成',
    sub: '基于案件事实与原文引证，快速起草规范法律文书',
    features: [
      { ic: 'i-doc', name: '起诉状', desc: '按规范格式起草', q: '请根据案件事实起草一份民事起诉状。' },
      { ic: 'i-shield', name: '答辩状', desc: '围绕争议焦点', q: '请生成一份答辩状框架。' },
      { ic: 'i-edit', name: '律师函', desc: '正式严谨措辞', q: '请起草一份催款律师函。' },
      { ic: 'i-layers', name: '合同起草', desc: '定制条款结构', q: '请起草一份借款合同初稿。' }
    ],
    suggests: ['请根据案件事实起草一份民事起诉状。', '请生成一份答辩状框架。', '请起草一份催款律师函。']
  }
}

export const badge = (t: string): string =>
  t === 'docx' ? 'W' : t === 'pdf' ? 'P' : t === 'xlsx' ? 'X' : t === 'md' ? 'M' : t === 'txt' ? 'T' : 'I'

export const CASE_TYPE_LABEL: Record<string, string> = {
  criminal: '刑事',
  civil: '民事',
  commercial: '商事',
  administrative: '行政',
  contract: '非诉合同',
  other: '其他'
}
