import type { SkillInfo } from '@lawbuddy/contracts'

/**
 * 技能的展示层中文映射（REQ-AI-003）。
 * 只影响 UI 文案，不改任何数据；未命中映射时回退原始名称。
 */

/** 已安装技能名的中文展示名（技能名来自 SKILL.md frontmatter，可能是英文 slug） */
const SKILL_LABELS: Record<string, string> = {
  'customs-trade-compliance': '海关贸易合规'
}

export interface CapabilityDisplay {
  /** 菜单条目/芯片上展示的名称（中文优先） */
  title: string
  /** 行内一句话简介（菜单灰色文字）；无简介为 null */
  desc: string | null
  /** 悬停说明卡正文（完整说明） */
  detail: string
}

export function skillDisplay(s: Pick<SkillInfo, 'id' | 'name' | 'description'>): CapabilityDisplay {
  const title = SKILL_LABELS[s.name] ?? SKILL_LABELS[s.id] ?? s.name
  const desc = (s.description || '').trim()
  return { title, desc: desc || null, detail: desc || '提示词技能：安装自技能文件，选中后作为本轮任务的操作指引。' }
}
