import type { JSX } from 'react'
import { MODES, useApp, type ModeKey } from '../state/store'
import { Icon } from './Icon'

const ORDER: Array<{ key: ModeKey; ic: string }> = [
  { key: 'yj', ic: 'i-doc' },
  { key: 'ht', ic: 'i-shield' },
  { key: 'ws', ic: 'i-edit' }
]

/** 顶部居中三段式模式控件（设计规范 §5.3） */
export function ModeTabs(): JSX.Element | null {
  const { mode, setMode, currentCaseId } = useApp()
  // 文书生成模式不再展示顶部模式页签；其他模式任务中仍隐藏，避免误改模式。
  if (currentCaseId || mode === 'ws') return null
  return (
    <div className="mode-tabs" role="tablist" aria-label="工作模式">
      {ORDER.map(({ key, ic }) => (
        <button
          key={key}
          className={`mode-tab${mode === key ? ' active' : ''}`}
          role="tab"
          aria-selected={mode === key}
          title={MODES[key].name}
          onClick={() => setMode(key)}
        >
          <Icon id={ic} />
          <span className="mt-label">{MODES[key].name}</span>
        </button>
      ))}
    </div>
  )
}
