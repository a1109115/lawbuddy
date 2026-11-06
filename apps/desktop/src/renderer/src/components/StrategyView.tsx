import { useState, type JSX } from 'react'
import type { StrategySpec } from '@lawbuddy/contracts'
import { useApp } from '../state/store'
import { CiteBadge } from './CiteBadge'
import { Icon } from './Icon'

/**
 * 辩护策略渲染器（PRD 13.6 / REQ-STR）：
 * - 有利/不利条件分组完整呈现（REQ-STR-004），逐项引证；
 * - 方案比较卡：前提/依据/行动/收益/风险/对方回应（REQ-STR-002/003）；
 * - AI 不自动选定最终方案 —— 用户点击「确认采用」经 strategy.decide 写入审计（REQ-STR-005）。
 */

function CondList({
  title,
  icon,
  tone,
  items,
  onCite
}: {
  title: string
  icon: string
  tone: 'fav' | 'unfav'
  items: Array<{ text: string; cite?: Parameters<typeof CiteBadge>[0]['cite'] }>
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element | null {
  if (items.length === 0) return null
  return (
    <div className="rr-sec str-cond">
      <div className="rr-sec-h">
        <Icon id={icon} />
        {title}
      </div>
      {items.map((it, i) => (
        <div key={i} className={`str-cond-item ${tone}`}>
          <span className={`st-badge ${tone === 'fav' ? 'st-favorable' : 'st-unfavorable'}`}>
            {tone === 'fav' ? '有利' : '不利'}
          </span>
          <span className="rr-fact-text">{it.text}</span>
          <CiteBadge cite={it.cite} onCite={onCite} />
        </div>
      ))}
    </div>
  )
}

function ListSection({
  title,
  icon,
  items,
  ordered
}: {
  title: string
  icon: string
  items: string[]
  ordered?: boolean
}): JSX.Element | null {
  if (items.length === 0) return null
  return (
    <div className="rr-sec">
      <div className="rr-sec-h">
        <Icon id={icon} />
        {title}
      </div>
      <ul className={`rr-list${ordered ? ' ol' : ''}`}>
        {items.map((x, i) => (
          <li key={i}>{x}</li>
        ))}
      </ul>
    </div>
  )
}

export function StrategyView({
  spec,
  taskId,
  onCite
}: {
  spec: StrategySpec
  taskId?: string
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  const decideStrategy = useApp((s) => s.decideStrategy)
  const [decided, setDecided] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const confirm = async (title: string): Promise<void> => {
    if (!taskId || decided || busy) return
    setBusy(true)
    const ok = await decideStrategy(taskId, title)
    setBusy(false)
    if (ok) setDecided(title)
  }

  return (
    <div className="str" aria-label="辩护策略分析">
      <div className="str-position">
        <Icon id="i-user" />
        {spec.position || '委托目标与限制未说明'}
      </div>

      {spec.warnings?.map((w) => (
        <div key={w} className="viz-warn">
          <Icon id="i-alert" />
          {w}
        </div>
      ))}

      {spec.keyDisputes.length > 0 && (
        <div className="str-disputes" aria-label="关键争点">
          {spec.keyDisputes.map((d, i) => (
            <span key={i} className="str-dispute">
              <Icon id="i-bulb" />
              {d}
            </span>
          ))}
        </div>
      )}

      <div className="str-conds">
        <CondList title="有利条件" icon="i-check" tone="fav" items={spec.favorable} onCite={onCite} />
        <CondList title="不利条件（必须完整呈现）" icon="i-alert" tone="unfav" items={spec.unfavorable} onCite={onCite} />
      </div>

      <ListSection title="信息缺口" icon="i-search" items={spec.infoGaps} />

      {spec.plans.length > 0 ? (
        <div className="str-plans" aria-label="方案比较">
          <div className="rr-sec-h">方案比较（{spec.plans.length} 个，AI 不替你选定最终方案）</div>
          {spec.plans.map((p, i) => (
            <div key={i} className={`str-plan${decided === p.title ? ' decided' : ''}`} aria-label={`方案：${p.title}`}>
              <div className="str-plan-head">
                <span className="str-plan-no">方案 {i + 1}</span>
                <b>{p.title}</b>
                <CiteBadge cite={p.cite} onCite={onCite} />
                {taskId && (
                  <button
                    className={`btn-primary str-confirm${decided === p.title ? ' done' : ''}`}
                    disabled={!!decided || busy}
                    title={decided === p.title ? '已写入审计' : '确认采用此方案（写入审计，REQ-STR-005）'}
                    onClick={() => void confirm(p.title)}
                  >
                    {decided === p.title ? '已确认采用' : decided ? '已选其他方案' : '确认采用'}
                  </button>
                )}
              </div>
              {p.premises.length > 0 && (
                <div className="str-block">
                  <span className="str-k">前提</span>
                  <ul className="rr-list ol">
                    {p.premises.map((x, j) => (
                      <li key={j}>{x}</li>
                    ))}
                  </ul>
                </div>
              )}
              {p.legalBasis.length > 0 && (
                <div className="str-block">
                  <span className="str-k">依据</span>
                  <ul className="rr-list">
                    {p.legalBasis.map((x, j) => (
                      <li key={j}>{x}</li>
                    ))}
                  </ul>
                </div>
              )}
              {p.actions.length > 0 && (
                <div className="str-block">
                  <span className="str-k">行动</span>
                  <ul className="rr-list ol">
                    {p.actions.map((x, j) => (
                      <li key={j}>{x}</li>
                    ))}
                  </ul>
                </div>
              )}
              {(p.benefits || p.risks || p.counter) && (
                <div className="str-blocks">
                  {p.benefits && (
                    <div className="str-mini ok">
                      <span className="str-k">
                        <Icon id="i-check" />
                        收益
                      </span>
                      {p.benefits}
                    </div>
                  )}
                  {p.risks && (
                    <div className="str-mini risk">
                      <span className="str-k">
                        <Icon id="i-alert" />
                        风险
                      </span>
                      {p.risks}
                    </div>
                  )}
                  {p.counter && (
                    <div className="str-mini counter">
                      <span className="str-k">
                        <Icon id="i-shield" />
                        对方可能主张
                      </span>
                      {p.counter}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
          {spec.plans.length === 1 && spec.singlePlanReason && (
            <div className="degrade-note">单一方案原因：{spec.singlePlanReason}</div>
          )}
        </div>
      ) : (
        spec.singlePlanReason && <div className="degrade-note">未生成方案：{spec.singlePlanReason}</div>
      )}

      <ListSection title="证据补充与程序动作" icon="i-plus" items={spec.evidenceActions} ordered />
      <ListSection title="需律师决定的事项" icon="i-user" items={spec.userDecisions} ordered />
    </div>
  )
}
