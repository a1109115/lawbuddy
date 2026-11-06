import { type JSX } from 'react'
import type { EvidenceCard, EvidenceSpec } from '@lawbuddy/contracts'
import { CiteBadge } from './CiteBadge'
import { Icon } from './Icon'

/**
 * 证据分析渲染器（PRD 13.5 / REQ-EVI）：
 * - 证据卡：材料记载字段与分析意见字段分区展示（REQ-EVI-001），
 *   证明目的可回原文（REQ-EVI-002，卡级引证芯片）；
 * - 合法性无法判断时显示待核实徽章（REQ-EVI-004，取得程序材料缺失不下确定结论）；
 * - 争点证据矩阵：支持/反对/冲突/缺失四种关系（REQ-EVI-003）。
 */

const MATRIX_LABEL: Record<string, string> = {
  supports: '支持',
  opposes: '反对',
  conflict: '冲突',
  missing: '缺失'
}

export function EvidenceView({
  spec,
  onCite
}: {
  spec: EvidenceSpec
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  return (
    <div className="evi" aria-label="证据分析">
      {spec.warnings?.map((w) => (
        <div key={w} className="viz-warn">
          <Icon id="i-alert" />
          {w}
        </div>
      ))}

      {spec.evidence.length > 0 ? (
        <div className="evi-cards">
          {spec.evidence.map((c, i) => (
            <EvidenceCardView key={`${c.name}-${i}`} card={c} onCite={onCite} />
          ))}
        </div>
      ) : (
        <div className="viz-empty">材料中未提取到可识别的证据。</div>
      )}

      {spec.matrix.disputes.length > 0 && <MatrixView spec={spec} />}
    </div>
  )
}

function Field({ label, value }: { label: string; value?: string }): JSX.Element | null {
  if (!value) return null
  return (
    <div className="evi-field">
      <span className="evi-k">{label}</span>
      <span className="evi-v">{value}</span>
    </div>
  )
}

function EvidenceCardView({
  card,
  onCite
}: {
  card: EvidenceCard
  onCite: (docId: string, pageNo: number) => void
}): JSX.Element {
  return (
    <div className="evi-card" aria-label={`证据卡：${card.name}`}>
      <div className="evi-head">
        <span className="rr-role">{card.type || '证据'}</span>
        <b className="rr-name">{card.name}</b>
        {card.legalityPending && (
          <span className="st-badge fst-pending" title="取得程序材料缺失，合法性无法判断（REQ-EVI-004）">
            合法性待核实
          </span>
        )}
        <CiteBadge cite={card.cite} onCite={onCite} />
      </div>

      {/* 材料记载字段（REQ-EVI-001：与 AI 判断分开展示） */}
      <div className="evi-sec" aria-label="材料记载">
        <div className="evi-sec-h">材料记载</div>
        <Field label="形成时间" value={card.formedAt} />
        <Field label="提供方" value={card.provider} />
        <Field label="保管情况" value={card.custody} />
        <Field label="证明目的" value={card.purpose} />
        {card.keyFragment && (
          <div className="evi-field">
            <span className="evi-k">关键片段</span>
            <span className="evi-v quote">「{card.keyFragment}」</span>
          </div>
        )}
      </div>

      {/* 分析意见字段（REQ-EVI-001：独立样式，不与材料记载混同） */}
      {(card.authenticity || card.legality || card.relevance || card.weight) && (
        <div className="evi-sec opinion" aria-label="分析意见">
          <div className="evi-sec-h">
            <Icon id="i-bulb" />
            分析意见（AI 判断，区别于材料记载）
          </div>
          <Field label="真实性" value={card.authenticity} />
          <div className="evi-field">
            <span className="evi-k">合法性</span>
            <span className="evi-v">
              {card.legality || '无法判断'}
              {card.legalityPending && <span className="evi-pending-mark">（待人工核实）</span>}
            </span>
          </div>
          <Field label="关联性" value={card.relevance} />
          <Field label="证明力" value={card.weight} />
        </div>
      )}

      {(card.relations.length > 0 || card.defects || card.reinforce || card.crossExam) && (
        <div className="evi-extra">
          {card.relations.length > 0 && (
            <div className="evi-rel">
              {card.relations.map((r, i) => (
                <span key={i} className={`evi-rel-chip${r.relation.includes('矛盾') ? ' warn' : ''}`}>
                  {r.relation.includes('矛盾') ? <Icon id="i-alert" /> : <Icon id="i-link" />}
                  {card.name} ↔ {r.to} · {r.relation}
                </span>
              ))}
            </div>
          )}
          <Field label="矛盾与缺陷" value={card.defects} />
          <Field label="补强建议" value={card.reinforce} />
          <Field label="质证问题" value={card.crossExam} />
        </div>
      )}
    </div>
  )
}

function MatrixView({ spec }: { spec: EvidenceSpec }): JSX.Element {
  const { disputes, cells } = spec.matrix
  const evidences = spec.evidence.map((c) => c.name)
  const cellMap = new Map<string, string>()
  const noteMap = new Map<string, string>()
  for (const c of cells) {
    cellMap.set(`${c.dispute}::${c.evidence}`, c.relation)
    if (c.note) noteMap.set(`${c.dispute}::${c.evidence}`, c.note)
  }
  return (
    <div className="evi-matrix" aria-label="争点证据矩阵">
      <div className="evi-matrix-h">争点证据矩阵（支持 / 反对 / 冲突 / 缺失）</div>
      <div className="rg-scroll">
        <table className="viz-table matrix" aria-label="争点与证据的二维关系">
          <thead>
            <tr>
              <th>争点 \ 证据</th>
              {evidences.map((e) => (
                <th key={e} title={e}>
                  {e.length > 8 ? `${e.slice(0, 8)}…` : e}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {disputes.map((d) => (
              <tr key={d}>
                <td className="evi-dispute">{d}</td>
                {evidences.map((e) => {
                  const rel = cellMap.get(`${d}::${e}`)
                  const note = noteMap.get(`${d}::${e}`)
                  return (
                    <td key={e} className="evi-cell" title={note ? `${MATRIX_LABEL[rel ?? ''] ?? ''} · ${note}` : MATRIX_LABEL[rel ?? ''] ?? '未标注'}>
                      {rel ? (
                        <span className={`mx mx-${rel}`} title={MATRIX_LABEL[rel] ?? rel}>
                          {rel === 'supports' ? (
                            <Icon id="i-check" />
                          ) : rel === 'opposes' ? (
                            <Icon id="i-x" />
                          ) : (
                            <Icon id="i-alert" />
                          )}
                          {MATRIX_LABEL[rel] ?? rel}
                        </span>
                      ) : (
                        <span className="mx mx-missing">—</span>
                      )}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="evi-legend">
        <span className="mx mx-supports">
          <Icon id="i-check" />
          支持
        </span>
        <span className="mx mx-opposes">
          <Icon id="i-x" />
          反对
        </span>
        <span className="mx mx-conflict">
          <Icon id="i-alert" />
          冲突
        </span>
        <span className="mx mx-missing">— 缺失</span>
      </div>
    </div>
  )
}
