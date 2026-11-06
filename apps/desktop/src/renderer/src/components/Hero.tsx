import type { JSX } from 'react'
import logoMark from '../assets/lawbuddy-hero.png'
import { MODES, YJ_PRESETS, useApp } from '../state/store'
import { Icon } from './Icon'

/** 空状态 Hero（设计规范 §5.3）：Logo + 问候 + 功能卡 + 建议问题 */
export function Hero(): JSX.Element {
  const { mode, chatting, messages, currentCaseId, setInput, setPreset } = useApp()
  const cfg = MODES[mode]

  const fill = (q: string): void => {
    setInput(q)
    document.querySelector<HTMLTextAreaElement>('.dock-input')?.focus()
  }

  // 法律阅卷功能卡：选择工作流预设并填充默认问题（PRD 13 功能入口）
  const onFeature = (name: string, q: string): void => {
    if (mode === 'yj') {
      const p = YJ_PRESETS.find((x) => x.name === name)
      if (p) setPreset(p.key)
    }
    fill(q)
  }

  return (
    // 空状态 Hero：仅在没有对话进行中且没有历史消息（如打开历史会话）时显示
    <section className={`hero${chatting || messages.length > 0 ? ' hide' : ''}`}>
      <img className="hero-logo" src={logoMark} alt="" />
      <h1 className="hero-title">你好，我是 LawBuddy</h1>
      <p className="hero-sub">
        {currentCaseId ? cfg.sub : `${cfg.sub} · 请先在左侧选择或新建任务`}
      </p>
      <div className="feature-row">
        {cfg.features.map((f) => (
          <button key={f.name} className="feature-card" onClick={() => onFeature(f.name, f.q)}>
            <span className="feature-ic">
              <Icon id={f.ic} />
            </span>
            <div className="feature-name">{f.name}</div>
            <div className="feature-desc">{f.desc}</div>
          </button>
        ))}
      </div>
      <div className="suggest-list">
        {cfg.suggests.map((s) => (
          <button key={s} className="suggest-chip" onClick={() => fill(s)}>
            <span>{s}</span>
            <span className="suggest-arrow">
              <Icon id="i-arrow" />
            </span>
          </button>
        ))}
      </div>
    </section>
  )
}
