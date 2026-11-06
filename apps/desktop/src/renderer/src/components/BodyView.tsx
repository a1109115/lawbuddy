import type { JSX } from 'react'
import type { AiMessageBody, ReviewReport } from '@lawbuddy/contracts'
import { CiteBadge } from './CiteBadge'
import { ContractReviewView } from './ContractReviewView'
import { DocGenMessageView } from './DocGenView'
import { EvidenceView } from './EvidenceView'
import { Icon } from './Icon'
import { StrategyView } from './StrategyView'
import { VisualizationView } from './VisualizationView'

const STANCE_LABEL: Record<string, string> = {
  favorable: '有利',
  unfavorable: '不利',
  neutral: '中立'
}

const STATUS_LABEL: Record<string, string> = {
  confirmed: '已确认',
  pending: '待核实'
}

/**
 * 分析结果统一渲染（ChatView 消息流与 ReadingView 产物共用）：
 * 阅卷报告 / 可视化 / 证据分析 / 策略 / 精简要点。
 */
export function BodyView({
  body,
  onCite,
  taskId,
  onPending
}: {
  body: AiMessageBody
  onCite: (docId: string, pageNo: number) => void
  taskId?: string
  /** 待核实芯片点击打开核实队列（缺省回落跳原文） */
  onPending?: () => void
}): JSX.Element {
  return (
    <>
      {body.contract ? (
        <ContractReviewView body={body.contract} reviewId={body.contract.reviewId ?? undefined} />
      ) : body.docgen ? (
        <DocGenMessageView body={body.docgen} />
      ) : body.answer ? (
        /* chat 产物（自主循环收尾）：对话分析全文 + 引证芯片 */
        <>
          <div className="doc-p" style={{ whiteSpace: 'pre-wrap' }}>{body.answer}</div>
          {body.cites.length > 0 && (
            <div className="cite-row">
              {body.cites.map((c, i) => (
                <CiteBadge key={`${c.claimId ?? c.documentId ?? i}-${i}`} cite={c} onCite={onCite} onPending={onPending} />
              ))}
            </div>
          )}
        </>
      ) : body.viz ? (
        <VisualizationView viz={body.viz} onCite={onCite} />
      ) : body.evidence ? (
        <EvidenceView spec={body.evidence} onCite={onCite} />
      ) : body.strategy ? (
        <StrategyView spec={body.strategy} taskId={taskId} onCite={onCite} />
      ) : body.report ? (
        <ReportView report={body.report} onCite={onCite} onPending={onPending} />
      ) : (
        <>
          {body.intro}
          {body.points && body.points.length > 0 && (
            <ol>
              {body.points.map((p, i) => (
                <li key={`${p.title}-${i}`} className="focus-item">
                  <b>{p.title}</b> —— {p.desc}
                </li>
              ))}
            </ol>
          )}
          {body.cites.length > 0 && (
            <div className="cite-row">
              {body.cites.map((c, i) => (
                <CiteBadge key={`${c.claimId ?? c.documentId ?? i}-${i}`} cite={c} onCite={onCite} onPending={onPending} />
              ))}
            </div>
          )}
        </>
      )}
      {body.degradedReason && (
        <div className="degrade-note">已降级为本地检索分析（未调用云端模型）</div>
      )}
      <div className="audit-note">
        <Icon id="i-alert" />
        {body.audit}
      </div>
    </>
  )
}

// ---------------------------------------------------------------------------
// 阅卷报告全规格渲染（PRD 13.3 十一章节；逐条目引证芯片 + 待人工核实徽章）
// ---------------------------------------------------------------------------

function ReportSection({
  title,
  icon,
  children
}: {
  title: string
  icon: string
  children: JSX.Element | Array<JSX.Element | null>
}): JSX.Element {
  return (
    <div className="rr-sec">
      <div className="rr-sec-h">
        <Icon id={icon} />
        {title}
      </div>
      {children}
    </div>
  )
}

function ReportView({
  report,
  onCite,
  onPending
}: {
  report: ReviewReport
  onCite: (docId: string, pageNo: number) => void
  onPending?: () => void
}): JSX.Element {
  return (
    <div className="report" aria-label="阅卷分析报告">
      <div className="rr-overview">{report.overview}</div>

      {report.parties.length > 0 && (
        <ReportSection title="当事人与关键主体" icon="i-user">
          {report.parties.map((p, i) => (
            <div key={`${p.name}-${i}`} className="rr-item">
              <span className="rr-role">{p.role || '当事人'}</span>
              <b className="rr-name">{p.name}</b>
              {p.note && <span className="rr-note">{p.note}</span>}
              <CiteBadge cite={p.cite} onCite={onCite} onPending={onPending} />
            </div>
          ))}
        </ReportSection>
      )}

      {report.timeline.length > 0 && (
        <ReportSection title="程序与事实时间线" icon="i-clock">
          {report.timeline.map((t, i) => (
            <div key={`${t.date}-${i}`} className="rr-tl-item">
              <span className="rr-tl-date">{t.date || '时间待定'}</span>
              <div className="rr-tl-body">
                <span>{t.event}</span>
                <CiteBadge cite={t.cite} onCite={onCite} onPending={onPending} />
              </div>
            </div>
          ))}
        </ReportSection>
      )}

      {report.disputes.length > 0 && (
        <ReportSection title="指控、诉请与争议焦点" icon="i-bulb">
          {report.disputes.map((d, i) => (
            <div key={`${d.title}-${i}`} className="rr-item col">
              <div className="rr-item-head">
                <b>{d.title}</b>
                <CiteBadge cite={d.cite} onCite={onCite} onPending={onPending} />
              </div>
              {d.desc && <span className="rr-note">{d.desc}</span>}
            </div>
          ))}
        </ReportSection>
      )}

      {report.facts.length > 0 && (
        <ReportSection title="事实认定（有利 / 不利 / 待核实）" icon="i-doc">
          {report.facts.map((f, i) => (
            <div key={`${f.text}-${i}`} className="rr-fact">
              <div className="rr-fact-badges">
                <span className={`st-badge st-${f.stance}`}>{STANCE_LABEL[f.stance] ?? f.stance}</span>
                <span className={`st-badge fst-${f.status}`}>{STATUS_LABEL[f.status] ?? f.status}</span>
              </div>
              <span className="rr-fact-text">{f.text}</span>
              <CiteBadge cite={f.cite} onCite={onCite} onPending={onPending} />
            </div>
          ))}
        </ReportSection>
      )}

      {report.contradictions.length > 0 && (
        <ReportSection title="材料矛盾与可能解释" icon="i-alert">
          {report.contradictions.map((c, i) => (
            <div key={`${c.topic}-${i}`} className="rr-vs">
              <div className="rr-vs-topic">{c.topic}</div>
              <div className="rr-vs-sides">
                <div className="rr-side">
                  {c.sideA}
                  <CiteBadge cite={c.citeA} onCite={onCite} onPending={onPending} />
                </div>
                <span className="rr-vs-mark" aria-hidden="true">
                  vs
                </span>
                <div className="rr-side b">
                  {c.sideB}
                  <CiteBadge cite={c.citeB} onCite={onCite} onPending={onPending} />
                </div>
              </div>
              {c.note && <div className="rr-vs-note">{c.note}</div>}
            </div>
          ))}
        </ReportSection>
      )}

      {report.missing.length > 0 && (
        <ReportSection title="缺失材料与补充建议" icon="i-plus">
          <ul className="rr-list">
            {report.missing.map((m, i) => (
              <li key={`${m}-${i}`}>{m}</li>
            ))}
          </ul>
        </ReportSection>
      )}

      {report.legalBasis.length > 0 && (
        <ReportSection title="法律依据与类案参考" icon="i-book">
          {report.legalBasis.map((l, i) => (
            <div key={`${l.title}-${i}`} className="rr-item">
              <b className="rr-name">{l.title}</b>
              {l.point && <span className="rr-note">{l.point}</span>}
              <CiteBadge cite={l.cite} onCite={onCite} onPending={onPending} />
            </div>
          ))}
        </ReportSection>
      )}

      {report.nextSteps.length > 0 && (
        <ReportSection title="下一步工作事项" icon="i-arrow">
          <ol className="rr-list ol">
            {report.nextSteps.map((n, i) => (
              <li key={`${n}-${i}`}>{n}</li>
            ))}
          </ol>
        </ReportSection>
      )}
    </div>
  )
}
